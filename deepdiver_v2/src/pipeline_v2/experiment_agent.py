from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .figure_planner import build_figure_plan
from .figure_critic import audit_figure, refine_spec
from .models import ReferenceRecord


logger = logging.getLogger(__name__)

DESCRIPTION_NAMES = (
    "总说明.txt", "数据说明.txt", "说明.txt", "说明文档.txt", "实验说明.txt",
    "description.txt", "readme.txt", "readme.md", "README.md",
)
ABLATION_TERMS = ("消融", "去掉", "移除", "删除", "不使用", "without", "remove", "no_", "no-")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _read_description_hierarchy(directory: Path, root: Path) -> Tuple[str, List[str]]:
    """Read explanations from experiment root down to the results.csv directory."""
    root = root.resolve()
    current = directory.resolve()
    if current != root and root not in current.parents:
        return "", []
    relative_parts = current.relative_to(root).parts
    directories = [root]
    cursor = root
    for part in relative_parts:
        cursor = cursor / part
        directories.append(cursor)
    chunks: List[str] = []
    files: List[str] = []
    for folder in directories:
        for name in DESCRIPTION_NAMES:
            path = folder / name
            if not path.is_file():
                continue
            text = _read_text(path)[:10000]
            if not text:
                continue
            relative = path.relative_to(root).as_posix()
            chunks.append(f"[{relative}]\n{text}")
            files.append(relative)
    return "\n\n".join(chunks), files


def _number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normal_column(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _metric_columns(fieldnames: Sequence[str], rows: Sequence[Dict[str, str]] = ()) -> Dict[str, str]:
    aliases = {
        "precision": ("metrics/precision", "precision"),
        "recall": ("metrics/recall", "recall"),
        "mAP50-95": ("metrics/map50-95", "map50-95", "map5095"),
        "mAP50": ("metrics/map50", "map50"),
        "accuracy": ("balanced_accuracy", "accuracy"),
        "F1": ("f1-score", "f1_score", "f1score", "f1"),
        "AUC": ("roc_auc", "auc"),
        "IoU": ("mean_iou", "miou", "iou"),
        "Dice": ("dice",),
        "R2": ("r_squared", "r2_score", "r2"),
        "RMSE": ("rmse",),
        "MAE": ("mae",),
        "MSE": ("mse",),
        "sensitivity": ("sensitivity",),
        "specificity": ("specificity",),
    }
    output: Dict[str, str] = {}
    normalized = {field: _normal_column(field).lower() for field in fieldnames}
    for label, candidates in aliases.items():
        for field, value in normalized.items():
            if label == "mAP50" and ("map50-95" in value or "map5095" in value):
                continue
            if any(candidate in value for candidate in candidates):
                output[label] = field
                break
    # Domain-general fallback: retain numeric outcome columns even when their
    # names are unknown to computer-vision aliases. Exclude obvious indices,
    # resource fields, hyperparameters and optimization losses.
    excluded = re.compile(
        r"epoch|iteration|\bstep\b|timestamp|\btime\b|date|year|seed|fold|batch|learning.?rate|\blr\b|loss|parameter|flops|memory",
        re.IGNORECASE,
    )
    used_fields = set(output.values())
    for field in fieldnames:
        if field in used_fields or excluded.search(_normal_column(field)):
            continue
        values = [str(row.get(field, "")).strip() for row in rows if str(row.get(field, "")).strip()]
        numeric = sum(_number(value) is not None for value in values)
        if values and numeric / len(values) >= 0.8:
            label = _clean_metric_label(field)
            if label and label not in output:
                output[label] = field
        if len(output) >= 12:
            break
    return output


def _clean_metric_label(field: str) -> str:
    label = re.sub(r"^(?:metrics?|val(?:idation)?|test|score)[/:_.-]+", "", _normal_column(field), flags=re.IGNORECASE)
    return label[:32] or _normal_column(field)[:32]


def _read_results(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            rows.append(dict(row))
    metrics = _metric_columns(fieldnames, rows)
    primary_label = next(
        (label for label in ("mAP50-95", "mAP50", "accuracy", "F1", "AUC", "precision", "recall", "IoU", "Dice", "R2", "RMSE", "MAE", "MSE") if label in metrics),
        next(iter(metrics), ""),
    )
    score_field = metrics.get(primary_label)
    best_index = 0
    if rows and score_field:
        lower_is_better = primary_label in {"RMSE", "MAE", "MSE"} or bool(re.search(r"error|loss", primary_label, re.I))
        finite_indices = [index for index in range(len(rows)) if _number(rows[index].get(score_field)) is not None]
        if finite_indices:
            selector = min if lower_is_better else max
            best_index = selector(finite_indices, key=lambda index: float(_number(rows[index].get(score_field))))
    best_row = rows[best_index] if rows else {}
    epoch_field = next((field for field in fieldnames if _normal_column(field).lower() == "epoch"), "")
    analysis = {
        "row_count": len(rows),
        "columns": fieldnames,
        "metric_columns": metrics,
        "primary_metric": primary_label,
        "metric_directions": {label: "min" if label in {"RMSE", "MAE", "MSE"} or re.search(r"error|loss", label, re.I) else "max" for label in metrics},
        "best_epoch": int(_number(best_row.get(epoch_field)) or best_index) if rows else None,
        "best_validation_metrics": {
            label: _number(best_row.get(field)) for label, field in metrics.items() if _number(best_row.get(field)) is not None
        },
        "metric_scope": "training_validation",
        "warning": "results.csv usually contains training-time validation metrics, not final held-out test metrics.",
    }
    return analysis, rows


def _configure_matplotlib():
    # PDF export can otherwise flood the workflow log with font subsetting details.
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib as mpl

    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "svg.fonttype": "none",
        "pdf.fonttype": 42,
        "font.size": 8,
        "axes.spines.right": False,
        "axes.spines.top": False,
        "legend.frameon": False,
    })


def _save_figure(fig, stem: Path) -> List[str]:
    stem.parent.mkdir(parents=True, exist_ok=True)
    outputs = []
    for suffix, kwargs in ((".png", {"dpi": 240}), (".svg", {}), (".pdf", {})):
        path = stem.with_suffix(suffix)
        fig.savefig(path, bbox_inches="tight", **kwargs)
        outputs.append(path.as_posix())
    return outputs


def _plot_training_curves(
    rows: Sequence[Dict[str, str]],
    analysis: Dict[str, Any],
    title: str,
    output_stem: Path,
) -> List[str]:
    if not rows:
        return []
    _configure_matplotlib()
    import matplotlib.pyplot as plt

    columns = analysis.get("columns") or []
    epoch_field = next((field for field in columns if _normal_column(field).lower() == "epoch"), "")
    epochs = [(_number(row.get(epoch_field)) if epoch_field else index) for index, row in enumerate(rows)]
    metric_columns = analysis.get("metric_columns") or {}
    loss_columns = [field for field in columns if "loss" in _normal_column(field).lower()][:8]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), constrained_layout=True)
    palette = ["#3568B8", "#4C9F70", "#D18B47", "#8E6BBE"]
    for color, (label, field) in zip(palette, metric_columns.items()):
        values = [_number(row.get(field)) for row in rows]
        axes[0].plot(epochs, values, label=label, color=color, linewidth=1.5)
    axes[0].set(title="Validation metrics", xlabel="Epoch", ylabel="Metric value", ylim=(0, 1.02))
    if metric_columns:
        axes[0].legend(fontsize=8)
    else:
        axes[0].text(0.5, 0.5, "No recognized metric columns", ha="center", va="center", transform=axes[0].transAxes)
    for index, field in enumerate(loss_columns):
        values = [_number(row.get(field)) for row in rows]
        axes[1].plot(epochs, values, label=_normal_column(field), linewidth=1.2, color=plt.cm.tab10(index % 10))
    axes[1].set(title="Optimization losses", xlabel="Epoch", ylabel="Loss")
    if loss_columns:
        axes[1].legend(fontsize=8)
    else:
        axes[1].text(0.5, 0.5, "No loss columns found", ha="center", va="center", transform=axes[1].transAxes)
    fig.suptitle(title, fontsize=10, fontweight="bold")
    outputs = _save_figure(fig, output_stem)
    plt.close(fig)
    return outputs


def _plot_metric_comparison(records: Sequence[Dict[str, Any]], output_stem: Path, title: str) -> List[str]:
    usable = [record for record in records if record.get("best_validation_metrics")]
    if len(usable) < 2:
        return []
    _configure_matplotlib()
    import matplotlib.pyplot as plt
    import numpy as np

    preferred = ("mAP50-95", "mAP50", "accuracy", "F1", "AUC", "precision", "recall", "IoU", "Dice", "R2", "RMSE", "MAE")
    available = []
    for record in usable:
        for metric in record["best_validation_metrics"]:
            if metric not in available:
                available.append(metric)
    metrics = ([metric for metric in preferred if metric in available] + [metric for metric in available if metric not in preferred])[:4]
    if not metrics:
        return []
    names = [str(record.get("display_name"))[:28] for record in usable]
    x = np.arange(len(names))
    width = 0.8 / len(metrics)
    fig_width = max(7.2, min(14, 0.55 * len(names) + 3.5))
    fig, ax = plt.subplots(figsize=(fig_width, 3.6), constrained_layout=True)
    colors = ["#3568B8", "#4C9F70", "#D18B47", "#8E6BBE"]
    for index, metric in enumerate(metrics):
        values = [record["best_validation_metrics"].get(metric, math.nan) for record in usable]
        ax.bar(x + (index - (len(metrics) - 1) / 2) * width, values, width, label=metric, color=colors[index], alpha=0.9)
    ax.set(title=title, ylabel="Training-time validation metric", xticks=x, xticklabels=names, ylim=(0, 1.02))
    ax.tick_params(axis="x", rotation=35, labelsize=8)
    ax.legend(ncol=min(4, len(metrics)), fontsize=8)
    outputs = _save_figure(fig, output_stem)
    plt.close(fig)
    return outputs


def _plot_planned_chart(rows: Sequence[Dict[str, str]], spec: Dict[str, Any], title: str, output_stem: Path) -> List[str]:
    if not rows:
        return []
    _configure_matplotlib()
    import matplotlib.pyplot as plt
    import numpy as np

    chart = spec["chart_type"]
    x_name, y_names = spec.get("x", ""), spec.get("y", [])
    fig, ax = plt.subplots(figsize=(6.2, 3.8), constrained_layout=True)
    colors = ["#3568B8", "#4C9F70", "#D18B47", "#8E6BBE", "#6B7280", "#C55A5A"]
    x_raw = [row.get(x_name, "") for row in rows] if x_name else []
    if chart == "line":
        x = [_number(v) for v in x_raw]
        for i, name in enumerate(y_names):
            ax.plot(x, [_number(row.get(name)) for row in rows], label=name, color=colors[i % len(colors)], lw=1.4)
        ax.set(xlabel=x_name, ylabel="Value")
        ax.legend(fontsize=8)
    elif chart in {"bar", "box"}:
        groups = list(dict.fromkeys(x_raw))
        values = {g: [_number(row.get(y_names[0])) for row in rows if row.get(x_name, "") == g] for g in groups}
        clean = [[v for v in values[g] if v is not None and math.isfinite(v)] for g in groups]
        if chart == "box":
            # Matplotlib rejects labels when one or more planned groups contain
            # no finite values. Keep labels and samples paired after cleaning.
            usable_groups = [(group, samples) for group, samples in zip(groups, clean) if samples]
            if not usable_groups:
                plt.close(fig)
                return []
            box_labels, box_values = zip(*usable_groups)
            ax.boxplot(list(box_values), labels=list(box_labels), showfliers=True)
        else:
            ax.bar(groups, [sum(v) / len(v) if v else math.nan for v in clean], color=colors[0])
        ax.set(xlabel=x_name, ylabel=y_names[0])
        ax.tick_params(axis="x", rotation=30, labelsize=8)
    elif chart in {"scatter", "actual_vs_predicted"}:
        x = np.array([_number(v) for v in x_raw], dtype=float)
        y = np.array([_number(row.get(y_names[0])) for row in rows], dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        ax.scatter(x[mask], y[mask], s=18, alpha=0.7, color=colors[0], edgecolors="none")
        if chart == "actual_vs_predicted" and mask.any():
            low, high = min(x[mask].min(), y[mask].min()), max(x[mask].max(), y[mask].max())
            ax.plot([low, high], [low, high], "--", color="#6B7280", lw=1, label="Ideal agreement")
            ax.legend(fontsize=8)
        ax.set(xlabel=x_name, ylabel=y_names[0])
    elif chart == "histogram":
        values = [_number(v) for v in x_raw]
        ax.hist([v for v in values if v is not None], bins="auto", color=colors[0], alpha=0.85)
        ax.set(xlabel=x_name, ylabel="Count")
    elif chart == "heatmap":
        matrix = np.array([[_number(row.get(name)) for name in y_names] for row in rows], dtype=float)
        matrix = matrix[np.all(np.isfinite(matrix), axis=1)]
        corr = np.corrcoef(matrix, rowvar=False) if len(matrix) >= 2 else np.eye(len(y_names))
        image = ax.imshow(corr, vmin=-1, vmax=1, cmap="coolwarm", aspect="auto")
        ax.set(xticks=range(len(y_names)), yticks=range(len(y_names)), xticklabels=y_names, yticklabels=y_names)
        ax.tick_params(axis="x", rotation=45, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        fig.colorbar(image, ax=ax, label="Correlation")
    else:
        plt.close(fig)
        return []
    ax.set_title(str(spec.get("purpose") or title), fontsize=9)
    fig.suptitle(title, fontsize=10, fontweight="bold")
    outputs = _save_figure(fig, output_stem)
    plt.close(fig)
    return outputs


class ExperimentAgent:
    """Discover experiments, inherit explanations, normalize metrics, and create deterministic figures."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "experiment_results"
        self.registry_path = self.root / "experiment_registry.json"
        self.figures_root = self.root / "figures"

    def run(
        self, query: str = "", references: Sequence[ReferenceRecord] = (), force_replan: bool = False,
        progress_callback=None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        self.root.mkdir(parents=True, exist_ok=True)
        previous: Dict[str, Dict[str, Any]] = {}
        if self.registry_path.exists():
            try:
                previous = {item["results_csv"]: item for item in json.loads(self.registry_path.read_text(encoding="utf-8"))}
            except Exception:
                previous = {}
        records: List[Dict[str, Any]] = []
        warnings: List[str] = []
        generated_names = {"experiment_registry.csv", "source_inventory.csv"}
        results_files = sorted(
            path for path in self.root.rglob("*.csv")
            if path.name.lower() not in generated_names and self.figures_root not in path.parents
        )
        logger.info("[ExperimentAgent] 发现 %s 个实验 CSV，开始逐个处理", len(results_files))
        if progress_callback:
            progress_callback("experiment_scan_started", {"total": len(results_files), "summary": f"发现 {len(results_files)} 个实验结果文件，准备逐个解析。"})
        for index, results_path in enumerate(results_files, 1):
            rel_path = results_path.relative_to(self.workspace_path).as_posix()
            digest = _sha256(results_path)
            description, description_files = _read_description_hierarchy(results_path.parent, self.root)
            old = previous.get(rel_path)
            experiment_id = f"EXP{index:04d}"
            expected_figure = self.figures_root / experiment_id / "training_curves.png"
            if old and not force_replan and old.get("sha256") == digest and old.get("description") == description and expected_figure.exists():
                records.append(old)
                logger.info("[ExperimentAgent] 复用缓存 %s/%s: %s", index, len(results_files), results_path.parent.name)
                if progress_callback:
                    progress_callback("experiment_file_ready", {"index": index, "total": len(results_files), "file": rel_path, "cached": True, "summary": f"复用已验证实验记录：{results_path.parent.name}"})
                continue
            try:
                logger.info("[ExperimentAgent] 处理 %s/%s: %s", index, len(results_files), rel_path)
                if progress_callback:
                    progress_callback("experiment_file_started", {"index": index, "total": len(results_files), "file": rel_path, "summary": f"我正在处理实验文件“{results_path.name}”，提取指标、训练曲线和实验设置。"})
                analysis, rows = _read_results(results_path)
                parent_label = results_path.parent.relative_to(self.root).as_posix()
                display_name = f"{parent_label} / {results_path.stem}" if parent_label != "." else results_path.stem
                figure_paths = _plot_training_curves(
                    rows,
                    analysis,
                    display_name,
                    self.figures_root / experiment_id / "training_curves",
                )
                figure_plan = build_figure_plan(
                    analysis.get("columns") or [], rows, analysis.get("metric_columns") or {}, references,
                    "\n".join(part for part in (query, description, display_name) if part),
                )
                planned_figures = []
                for chart_index, spec in enumerate(figure_plan.get("figures", []), 1):
                    spec, refinement_changes = refine_spec(spec, figure_plan.get("profile", {}))
                    paths = _plot_planned_chart(
                        rows, spec, display_name,
                        self.figures_root / experiment_id / f"planned_{chart_index:02d}_{spec['chart_type']}",
                    )
                    relative_paths = [Path(p).relative_to(self.workspace_path).as_posix() for p in paths]
                    review = audit_figure(spec, figure_plan.get("profile", {}), relative_paths, self.workspace_path)
                    planned_figures.append({
                        **spec, "files": relative_paths, "refinement_changes": refinement_changes,
                        "quality_review": review,
                    })
                provided_figures = []
                for suffix in (".png", ".jpg", ".jpeg", ".svg", ".pdf"):
                    candidate = results_path.with_suffix(suffix)
                    if candidate.is_file():
                        provided_figures.append(candidate.relative_to(self.workspace_path).as_posix())
                args_path = results_path.parent / "args.yaml"
                best_path = results_path.parent / "weights" / "best.pt"
                records.append({
                    "experiment_id": experiment_id,
                    "display_name": display_name,
                    "folder": results_path.parent.relative_to(self.workspace_path).as_posix(),
                    "folder_hierarchy": list(results_path.parent.relative_to(self.root).parts),
                    "results_csv": rel_path,
                    "sha256": digest,
                    "description": description,
                    "description_files": description_files,
                    "args_yaml": args_path.relative_to(self.workspace_path).as_posix() if args_path.exists() else "",
                    "best_weights": best_path.relative_to(self.workspace_path).as_posix() if best_path.exists() else "",
                    "figures": [Path(path).relative_to(self.workspace_path).as_posix() for path in figure_paths],
                    "provided_figures": provided_figures,
                    "figure_plan": {**figure_plan, "figures": planned_figures},
                    "status": "processed",
                    "needs_user_confirmation": not bool(description),
                    **analysis,
                })
                if progress_callback:
                    progress_callback("experiment_file_ready", {
                        "index": index, "total": len(results_files), "file": rel_path,
                        "chart_types": [item.get("chart_type") for item in planned_figures],
                        "summary": f"实验 {display_name} 已解析；规划图表：" + ("、".join(str(item.get("chart_type")) for item in planned_figures) or "训练曲线"),
                    })
            except Exception as exc:
                logger.exception("[ExperimentAgent] 实验处理失败: %s", rel_path)
                warnings.append(f"Could not process experiment CSV {rel_path}: {exc}")
                records.append({
                    "experiment_id": f"EXP{index:04d}", "display_name": results_path.stem,
                    "folder": results_path.parent.relative_to(self.workspace_path).as_posix(),
                    "results_csv": rel_path, "sha256": digest, "description": description,
                    "description_files": description_files, "status": "failed", "error": str(exc),
                })

        figure_manifest: List[Dict[str, Any]] = []
        for page, start in enumerate(range(0, len(records), 15), 1):
            subset = records[start:start + 15]
            paths = _plot_metric_comparison(
                subset,
                self.figures_root / f"experiment_metric_comparison_{page:02d}",
                "Experiment comparison (validation metrics)",
            )
            if paths:
                figure_manifest.append({
                    "type": "comparison", "scope": [item.get("experiment_id") for item in subset],
                    "conclusion": "Compare training-time validation performance across experiments without treating it as held-out test evidence.",
                    "files": [Path(path).relative_to(self.workspace_path).as_posix() for path in paths],
                })
        ablations = [record for record in records if any(term in f"{record.get('display_name', '')} {record.get('description', '')}".lower() for term in ABLATION_TERMS)]
        if len(ablations) >= 2:
            paths = _plot_metric_comparison(
                ablations,
                self.figures_root / "ablation_metric_comparison",
                "Ablation comparison (validation metrics)",
            )
            if paths:
                figure_manifest.append({
                    "type": "ablation", "scope": [item.get("experiment_id") for item in ablations],
                    "conclusion": "Compare ablation variants using the same reported validation metrics; causal interpretation requires matched settings.",
                    "files": [Path(path).relative_to(self.workspace_path).as_posix() for path in paths],
                })

        self.registry_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.root / "figure_plans.json").write_text(
            json.dumps([
                {"experiment_id": record.get("experiment_id"), "display_name": record.get("display_name"),
                 "figure_plan": record.get("figure_plan", {})}
                for record in records
            ], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.root / "figure_manifest.json").write_text(json.dumps(figure_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# Experiment Registry", ""]
        for record in records:
            lines.extend([
                f"## {record['experiment_id']} · {record['display_name']}",
                f"- Status: {record.get('status')}",
                f"- Source: `{record.get('results_csv')}`",
                f"- Explanation chain: {', '.join(record.get('description_files') or []) or '需要用户确认'}",
                f"- Description: {record.get('description') or '需要用户确认'}",
                f"- Best validation epoch: {record.get('best_epoch')}",
                f"- Best validation metrics: {json.dumps(record.get('best_validation_metrics', {}), ensure_ascii=False)}",
                f"- Figures: {', '.join(record.get('figures') or []) or 'none'}",
                f"- Planned figure types: {', '.join(item.get('chart_type', '') for item in record.get('figure_plan', {}).get('figures', [])) or 'none'}",
                "",
            ])
        (self.root / "experiment_summary.md").write_text("\n".join(lines), encoding="utf-8")
        logger.info("[ExperimentAgent] 完成：%s 个实验，%s 组汇总图，%s 个警告", len(records), len(figure_manifest), len(warnings))
        return records, warnings
