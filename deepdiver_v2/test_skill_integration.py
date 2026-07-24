#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 Skill 集成是否成功
运行此脚本验证 skills 是否正确加载
"""
import sys
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from deepdiver_v2.src.utils.console_encoding import force_utf8_console
from deepdiver_v2.src.utils.skill_loader import get_skill_loader

force_utf8_console()


def test_skill_loader():
    """测试 SkillLoader"""
    print("="*80)
    print("🧪 测试 1: SkillLoader 基础功能")
    print("="*80)
    
    try:
        loader = get_skill_loader()
        
        # 显示所有可用 skills
        available_skills = loader.get_skill_list()
        print(f"\n✅ 发现 {len(available_skills)} 个可用 skills")
        print(f"前 20 个 skills: {available_skills[:20]}")
        
        # 测试加载 scientific-writing skill
        print("\n" + "="*80)
        print("🧪 测试 2: 加载 scientific-writing skill")
        print("="*80)
        
        skill_content = loader.load_skill("scientific-writing")
        
        if skill_content:
            print(f"✅ 成功加载 scientific-writing skill")
            print(f"   内容长度: {len(skill_content)} 字符")
            print(f"   前 200 字符: {skill_content[:200]}...")
        else:
            print("❌ 加载 scientific-writing skill 失败")
        
        # 测试加载多个 skills
        print("\n" + "="*80)
        print("🧪 测试 3: 加载多个 skills")
        print("="*80)
        
        test_skills = ["scientific-writing"]
        combined = loader.load_multiple_skills(test_skills)
        
        if combined:
            print(f"✅ 成功加载 {len(test_skills)} 个 skills")
            print(f"   合并后长度: {len(combined)} 字符")
        else:
            print("❌ 加载多个 skills 失败")
        
        return True
        
    except Exception as e:
        print(f"❌ SkillLoader 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_writer_agent_integration():
    """测试 V2 Writer 使用的无网络 Skill 注入路径"""
    print("\n" + "="*80)
    print("🧪 测试 4: WriterAgent 集成 Skills")
    print("="*80)
    
    try:
        loader = get_skill_loader()
        enabled_skills = loader.select_skills_for_agent(
            "WriterAgent",
            task_text="请根据实验材料撰写学术论文",
            file_paths=["user_uploads/notes.txt"],
        )
        print(f"✅ V2 Writer Skill 路由创建成功")
        print(f"   启用的 skills: {enabled_skills}")
        
        # 构建系统提示词
        print("\n" + "="*80)
        print("🧪 测试 5: 生成系统提示词（含 skills）")
        print("="*80)
        
        system_prompt = loader.inject_agent_skills(
            "You are a careful academic writing assistant.",
            agent_name="WriterAgent",
            task_text="请根据实验材料撰写学术论文",
            file_paths=["user_uploads/notes.txt"],
            compact=True,
        )
        
        if system_prompt:
            print(f"✅ 系统提示词生成成功")
            print(f"   提示词长度: {len(system_prompt)} 字符")
            
            # 检查是否包含 skill 内容
            if "SCIENTIFIC WRITING SKILLS SUPPLEMENT" in system_prompt:
                print(f"   ✅ 提示词中包含 scientific-writing skill")
            else:
                print(f"   ⚠️  提示词中未找到 scientific-writing skill")
            
            if "### Scientific Writing" in system_prompt:
                print(f"   ✅ 提示词中包含科学写作规范")
            else:
                print(f"   ⚠️  提示词中未找到科学写作规范")
            
            # 显示前 500 字符
            print(f"\n   提示词前 500 字符:")
            print(f"   {'-'*80}")
            print(f"   {system_prompt[:500]}")
            print(f"   {'-'*80}")
        else:
            print("❌ 系统提示词生成失败")
        
        return True
        
    except Exception as e:
        print(f"❌ WriterAgent 集成测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n" + "="*80)
    print("🚀 SciAssistant Skill 集成测试")
    print("="*80 + "\n")
    
    # 运行测试
    test1_passed = test_skill_loader()
    test2_passed = test_writer_agent_integration()
    
    # 总结
    print("\n" + "="*80)
    print("📊 测试总结")
    print("="*80)
    
    if test1_passed and test2_passed:
        print("✅ 所有测试通过！Skill 集成成功！")
        print("\n🎉 现在 WriterAgent 将使用 scientific-writing skill 来生成更高质量的论文")
    else:
        print("❌ 部分测试失败，请检查错误信息")
    
    print("="*80)
