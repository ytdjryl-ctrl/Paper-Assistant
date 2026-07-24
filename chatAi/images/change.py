import os
import re


def upgrade_html():
    target_file = "ai_chat.html"

    if not os.path.exists(target_file):
        print(f"❌ 找不到文件: {target_file}")
        print("请将该脚本放在与 ai_chat.html 同级的目录下运行。")
        return

    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 0. 自动备份原文件，保证安全
    backup_file = "ai_chat_backup.html"
    with open(backup_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ 已自动备份原文件至: {backup_file}")

    # 1. 注入全局合法后缀名数组
    if "const globalAllowedExts" not in content:
        target = "window.addEventListener('DOMContentLoaded', function() {"
        replacement = "const globalAllowedExts = ['.txt','.md','.csv','.json','.log','.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.xml','.html','.htm','.rtf','.odt','.epub','.yaml','.yml','.jpg','.jpeg','.png','.bmp','.webp','.zip','.rar','.tar','.gz','.7z'];\n\n        " + target
        content = content.replace(target, replacement)
        print("✅ 注入全局格式白名单成功")

    # 2. 替换 HTML 原生标签的 accept 拦截
    new_accept = 'accept=".txt,.md,.csv,.json,.log,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.xml,.html,.htm,.rtf,.odt,.epub,.yaml,.yml,.jpg,.jpeg,.png,.bmp,.webp,.zip,.rar,.tar,.gz,.7z"'
    content = re.sub(r'accept="[^"]+"', new_accept, content)
    print("✅ 浏览器端原生上传标签校验解除成功")

    # 3. 调大上传限制至 500MB (以支持压缩包数据集)
    content = re.sub(r'const maxSingleSize = isDeepDiverMode \? 10 \* 1024 \* 1024 : 3 \* 1024 \* 1024;',
                     'const maxSingleSize = isDeepDiverMode ? 200 * 1024 * 1024 : 30 * 1024 * 1024;', content)
    content = re.sub(r'const maxTotalSize = isDeepDiverMode \? 60 \* 1024 \* 1024 : 20 \* 1024 \* 1024;',
                     'const maxTotalSize = isDeepDiverMode ? 500 * 1024 * 1024 : 60 * 1024 * 1024;', content)
    content = re.sub(r'const maxFileSize = 10 \* 1024 \* 1024;\s*// 10MB',
                     'const maxFileSize = 200 * 1024 * 1024; // 200MB', content)
    content = re.sub(r'const maxTotalSize = 60 \* 1024 \* 1024;\s*// 60MB',
                     'const maxTotalSize = 500 * 1024 * 1024; // 500MB', content)
    content = re.sub(r'const MAX_SIZE = 50 \* 1024 \* 1024;\s*// 最大总大小 50MB',
                     'const MAX_SIZE = 500 * 1024 * 1024; // 最大总大小 500MB', content)
    print("✅ 实验文件大小限制上调至 500MB 成功")

    # 4. 破解 DeepDiver/Experiment 模式的严格格式验证
    old_dd = r"const validExtensions = \['\.pdf', '\.doc', '\.docx', '\.txt'\];\s*const isValidType = validExtensions\.some\(ext => fileName\.endsWith\(ext\)\);"
    new_dd = """let isValidType = false;
                for (const ext of globalAllowedExts) {
                    if (fileName.endsWith(ext)) { isValidType = true; break; }
                }
                if (fileName.endsWith('.tar.gz')) isValidType = true;"""
    content = re.sub(old_dd, new_dd, content)

    # 5. 破解基础文件库的 MIME 严格验证
    old_lib = r"if \(!ALLOWED_TYPES\.includes\(file\.type\)\) {"
    new_lib = """let isValidType = false;
                const fileNameLower = file.name.toLowerCase();
                for (const ext of globalAllowedExts) {
                    if (fileNameLower.endsWith(ext)) { isValidType = true; break; }
                }
                if (fileNameLower.endsWith('.tar.gz')) isValidType = true;

                if (!isValidType) {"""
    content = re.sub(old_lib, new_lib, content)
    print("✅ 格式类型拦截器 (MIME & 后缀名) 破解成功")

    # 6. 更新压缩包和图片的精美图标
    old_icons = r"if \(fileExtension === 'pdf'\) \{ fileIcon = 'fa-file-pdf-o'; iconColor = 'text-red-500'; \}\s*else if \(\['doc', 'docx'\]\.includes\(fileExtension\)\) \{ fileIcon = 'fa-file-word-o'; iconColor = 'text-blue-500'; \}\s*else if \(\['xls', 'xlsx'\]\.includes\(fileExtension\)\) \{ fileIcon = 'fa-file-excel-o'; iconColor = 'text-green-500'; \}\s*else if \(\['ppt', 'pptx'\]\.includes\(fileExtension\)\) \{ fileIcon = 'fa-file-powerpoint-o'; iconColor = 'text-orange-500'; \}\s*else if \(\['jpg', 'jpeg', 'png'\]\.includes\(fileExtension\)\) \{ fileIcon = 'fa-file-image-o'; iconColor = 'text-purple-500'; \}\s*else if \(fileExtension === 'txt'\) \{ fileIcon = 'fa-file-text-o'; iconColor = 'text-gray-500'; \}"

    new_icons = """if (fileExtension === 'pdf') { fileIcon = 'fa-file-pdf-o'; iconColor = 'text-red-500'; }
                else if (['doc', 'docx'].includes(fileExtension)) { fileIcon = 'fa-file-word-o'; iconColor = 'text-blue-500'; }
                else if (['xls', 'xlsx', 'csv'].includes(fileExtension)) { fileIcon = 'fa-file-excel-o'; iconColor = 'text-green-500'; }
                else if (['ppt', 'pptx'].includes(fileExtension)) { fileIcon = 'fa-file-powerpoint-o'; iconColor = 'text-orange-500'; }
                else if (['jpg', 'jpeg', 'png', 'bmp', 'webp'].includes(fileExtension)) { fileIcon = 'fa-file-image-o'; iconColor = 'text-purple-500'; }
                else if (['zip', 'rar', 'tar', 'gz', '7z'].includes(fileExtension)) { fileIcon = 'fa-file-archive-o'; iconColor = 'text-yellow-600'; }
                else if (['txt', 'json', 'xml', 'html'].includes(fileExtension)) { fileIcon = 'fa-file-code-o'; iconColor = 'text-gray-500'; }"""

    content = re.sub(old_icons, new_icons, content)

    # 写入保存
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)

    print("🎉 升级大功告成！刷新网页，前端代码现已完美支持压缩包、图像及其他所有数据集格式！")


if __name__ == "__main__":
    upgrade_html()