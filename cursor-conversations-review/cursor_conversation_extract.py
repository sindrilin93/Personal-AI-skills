#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import hashlib
import datetime
import re
import glob
import tempfile
import shutil
import sys
from typing import List, Tuple, Dict, Optional

# ======================================
# 一、全局配置（唯一需要修改的地方）
# ======================================
# 脚本所在根目录（自动获取，无需手动修改）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 输出目录：存放最终的对话Markdown文件
OUTPUT_DIR = os.path.join(BASE_DIR, "reviews")
# 状态文件：记录增量读取位置，避免重复处理旧内容
STATE_FILE = os.path.join(BASE_DIR, "extract_state.json")
# 日志文件：详细记录运行过程
LOG_FILE = os.path.join(BASE_DIR, "extract_debug.log")
# Cursor对话文件匹配规则（Mac标准路径，无需修改）
CURSOR_TRANSCRIPT_GLOB = os.path.expanduser("~/.cursor/projects/*/agent-transcripts/*/*.jsonl")

# ======================================
# 二、基础工具函数（职责单一，无耦合）
# ======================================
def write_log(msg: str) -> None:
    """写日志文件，统一日志格式"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    log_line = f"[{timestamp}] {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line)

def get_md5(text: str) -> str:
    """生成MD5，用于去重和文件唯一标识"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def clean_empty_lines(text: str) -> str:
    """清理多余空行，最多保留1行空行"""
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# ======================================
# 三、内容净化核心函数
# ======================================
def protect_code_and_tables(text: str) -> Tuple[str, List[str]]:
    """保护代码块和表格，避免被清理规则破坏，返回(处理后的文本, 保护内容列表)"""
    protected_blocks = []
    placeholder_template = "___PROTECTED_BLOCK_{}___"

    # 1. 保护```包裹的代码块
    def save_code_block(match: re.Match) -> str:
        protected_blocks.append(match.group(0))
        return placeholder_template.format(len(protected_blocks) - 1)
    text = re.sub(r"```[\s\S]*?```", save_code_block, text, flags=re.MULTILINE)

    # 2. 保护Markdown表格
    def save_table(match: re.Match) -> str:
        protected_blocks.append(match.group(0))
        return placeholder_template.format(len(protected_blocks) - 1)
    text = re.sub(r"(^\|.*?\|$\n?)+", save_table, text, flags=re.MULTILINE)

    return text, protected_blocks

def restore_protected_blocks(text: str, protected_blocks: List[str]) -> str:
    """还原被保护的代码块和表格"""
    for idx, block in enumerate(protected_blocks):
        text = text.replace(f"___PROTECTED_BLOCK_{idx}___", block)
    return text

def clean_user_question(text: str) -> str:
    """净化用户提问：去除<user_query>标签，清理多余内容"""
    if not text or not isinstance(text, str):
        return ""
    # 去除<user_query>标签，保留内部内容
    text = re.sub(r"<user_query>([\s\S]*?)</user_query>", r"\1", text, flags=re.S)
    # 清理其他尖括号标签
    text = re.sub(r"<[^>]+>", "", text)
    # 清理多余空行
    return clean_empty_lines(text)

def clean_assistant_answer(text: str) -> str:
    """净化AI回答：去除thinking块、纯英文过程内容，保留最终中文结论"""
    if not text or not isinstance(text, str):
        return ""
    
    # 1. 彻底去除<thinking>标签及内部所有内容（支持跨行）
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text, flags=re.S)
    
    # 2. 保护代码块和表格
    text, protected_blocks = protect_code_and_tables(text)
    
    # 3. 过滤纯英文无效行（中文占比<3%的行直接丢弃）
    lines = text.split("\n")
    valid_lines = []
    for line in lines:
        stripped_line = line.strip()
        # 保留空行、占位符、Markdown格式行
        if not stripped_line or "___PROTECTED_BLOCK_" in stripped_line or stripped_line.startswith(("#", "- ", "* ", "---", "| ")):
            valid_lines.append(line)
            continue
        # 统计中文占比
        chinese_chars = re.findall(r"[\u4e00-\u9fa5]", stripped_line)
        chinese_ratio = len(chinese_chars) / len(stripped_line) if len(stripped_line) > 0 else 1.0
        # 保留中文占比≥3%的行
        if chinese_ratio >= 0.03:
            valid_lines.append(line)
    text = "\n".join(valid_lines)
    
    # 4. 还原保护内容
    text = restore_protected_blocks(text, protected_blocks)
    
    # 5. 最终清理
    return clean_empty_lines(text)

# ======================================
# 四、消息解析与核心对话合并逻辑
# ======================================
def parse_single_message(raw_msg: Dict) -> Optional[Dict]:
    """
    解析单条jsonl消息，只返回有效user/assistant消息，其他直接过滤
    返回格式: {"role": "user/assistant", "text": "净化后的文本"}
    """
    if not isinstance(raw_msg, dict):
        return None
    
    # 1. 提取角色，严格只保留user和assistant
    role = ""
    if "role" in raw_msg and isinstance(raw_msg["role"], str):
        role = raw_msg["role"].strip().lower()
    # 兼容嵌套message结构
    elif "message" in raw_msg and isinstance(raw_msg["message"], dict):
        role = raw_msg["message"].get("role", "").strip().lower()
    
    # 非user/assistant直接过滤，不进入后续流程
    if role not in ["user", "assistant"]:
        return None

    # 2. 提取消息文本内容，兼容Cursor的数组格式content
    message_body = raw_msg.get("message", {}) if isinstance(raw_msg.get("message"), dict) else {}
    raw_content = message_body.get("content", message_body.get("text", ""))
    
    final_text = ""
    # 处理数组格式的content（Cursor主流格式）
    if isinstance(raw_content, list):
        text_parts = []
        for item in raw_content:
            if isinstance(item, dict) and item.get("type") == "text":
                part_text = item.get("text", "")
                if isinstance(part_text, str):
                    text_parts.append(part_text)
        final_text = "\n".join(text_parts)
    # 处理字符串格式的content
    elif isinstance(raw_content, str):
        final_text = raw_content

    # 3. 按角色净化内容
    if role == "user":
        final_text = clean_user_question(final_text)
    elif role == "assistant":
        final_text = clean_assistant_answer(final_text)

    # 过滤空内容
    if not final_text:
        return None

    return {"role": role, "text": final_text}

def merge_consecutive_messages(messages: List[Dict]) -> List[Tuple[str, str]]:
    """
    【核心逻辑】合并连续消息，生成最终QA对
    规则1：连续的user消息，全部拼接成一个完整问题
    规则2：user之后的连续assistant消息，只保留最后一条作为回答
    返回格式: [ (完整问题, 最终回答), (完整问题, 最终回答), ... ]
    """
    qa_pairs = []
    if not messages:
        return qa_pairs

    # 状态变量
    current_question = ""  # 拼接中的完整用户问题
    current_answer = ""    # 缓存中的AI回答（只保留最后一条）
    is_collecting_user = True  # 当前状态：是否正在收集用户问题

    write_log(f"开始合并消息流，总有效消息数: {len(messages)}")

    for msg in messages:
        role = msg["role"]
        text = msg["text"]

        # 状态1：正在收集用户问题
        if is_collecting_user:
            if role == "user":
                # 连续user消息，拼接到当前问题
                if current_question:
                    current_question += "\n" + text
                else:
                    current_question = text
            elif role == "assistant":
                # 遇到第一条assistant，切换状态，开始收集回答
                is_collecting_user = False
                current_answer = text  # 覆盖，只留最后一条

        # 状态2：正在收集AI回答
        else:
            if role == "assistant":
                # 连续assistant消息，直接覆盖，只保留最后一条
                current_answer = text
            elif role == "user":
                # 遇到新的user消息，当前QA对收集完成，存入结果
                if current_question and current_answer:
                    qa_pairs.append((current_question, current_answer))
                    write_log(f"✅ 提取到1组有效QA，问题长度: {len(current_question)}，回答长度: {len(current_answer)}")
                # 重置状态，开始收集新的问题
                current_question = text
                current_answer = ""
                is_collecting_user = True

    # 遍历结束，处理最后一组未存入的QA对
    if current_question and current_answer:
        qa_pairs.append((current_question, current_answer))
        write_log(f"✅ 提取到最后1组有效QA，问题长度: {len(current_question)}，回答长度: {len(current_answer)}")

    write_log(f"消息流合并完成，共提取到 {len(qa_pairs)} 组有效QA对")
    return qa_pairs

# ======================================
# 五、文件IO与增量读取管理
# ======================================
def load_state() -> Dict:
    """加载增量读取状态，无状态文件则返回空字典"""
    if not os.path.exists(STATE_FILE):
        write_log("无状态文件，初始化空状态")
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            write_log(f"加载状态成功，已记录 {len(state)} 个文件")
            return state
    except Exception as e:
        write_log(f"状态文件加载失败，重置状态: {str(e)}")
        return {}

def save_state(state: Dict) -> None:
    """原子化保存增量状态，避免文件损坏"""
    temp_fd, temp_path = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        shutil.move(temp_path, STATE_FILE)
        write_log(f"状态保存成功，共记录 {len(state)} 个文件")
    except Exception as e:
        write_log(f"状态保存失败: {str(e)}")
        if os.path.exists(temp_path):
            os.remove(temp_path)

def find_all_transcript_files() -> List[str]:
    """查找所有符合规则的Cursor对话jsonl文件"""
    write_log(f"查找对话文件，匹配规则: {CURSOR_TRANSCRIPT_GLOB}")
    # 查找.jsonl文件
    jsonl_files = glob.glob(CURSOR_TRANSCRIPT_GLOB, recursive=False)
    # 兼容.json后缀
    json_files = glob.glob(CURSOR_TRANSCRIPT_GLOB.replace(".jsonl", ".json"), recursive=False)
    # 去重+排序
    all_files = sorted(list(set(jsonl_files + json_files)))
    
    if not all_files:
        write_log("❌ 未找到任何对话文件")
    else:
        write_log(f"✅ 找到 {len(all_files)} 个对话文件")
        for i, file in enumerate(all_files[:5]):
            write_log(f"  {i+1}. {os.path.basename(file)}")
        if len(all_files) > 5:
            write_log(f"  ... 还有 {len(all_files)-5} 个文件")
    return all_files

def read_file_incremental(file_path: str, state: Dict) -> Tuple[List[Dict], Dict]:
    """增量读取文件，只读取新增内容，返回解析后的有效消息列表和更新后的状态"""
    file_key = get_md5(file_path)
    file_state = state.get(file_key, {"offset": 0, "processed_line_hashes": []})
    
    # 获取文件当前大小
    file_size = os.path.getsize(file_path)
    # 处理文件被重写的情况
    if file_state["offset"] > file_size:
        write_log(f"⚠️ 文件被重写，重置偏移量: {os.path.basename(file_path)}")
        file_state = {"offset": 0, "processed_line_hashes": []}
    
    # 回退5000字节，避免截断json行
    read_offset = max(0, file_state["offset"] - 5000)
    processed_hashes = set(file_state["processed_line_hashes"])
    valid_messages = []
    new_line_hashes = []

    write_log(f"读取文件: {os.path.basename(file_path)}，起始偏移量: {read_offset}")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(read_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 计算行哈希，去重
            line_hash = get_md5(line)
            if line_hash in processed_hashes:
                continue
            new_line_hashes.append(line_hash)
            # 解析json行
            try:
                raw_msg = json.loads(line)
                parsed_msg = parse_single_message(raw_msg)
                if parsed_msg:
                    valid_messages.append(parsed_msg)
            except Exception as e:
                write_log(f"⚠️ 行解析失败: {str(e)}，行前50字符: {line[:50]}")
                continue
        # 记录最终偏移量
        final_offset = f.tell()

    # 更新文件状态
    updated_file_state = {
        "offset": final_offset,
        "processed_line_hashes": list(processed_hashes.union(set(new_line_hashes)))
    }
    state[file_key] = updated_file_state

    write_log(f"文件读取完成，新增有效消息数: {len(valid_messages)}")
    return valid_messages, state

def write_qa_to_markdown(qa_pairs: List[Tuple[str, str]]) -> None:
    """将QA对写入按日期命名的Markdown文件，自动去重"""
    if not qa_pairs:
        write_log("⚠️ 本次无新增QA对，跳过写入文件")
        return
    
    # 按日期生成输出文件
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    output_file = os.path.join(OUTPUT_DIR, f"{today_str}.md")
    
    # 读取现有文件，做去重处理
    existing_question_hashes = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                content = f.read()
                # 提取已有问题，生成哈希去重
                existing_questions = re.findall(r"## 问题\n(.*?)\n\n## 回答", content, flags=re.S)
                for q in existing_questions:
                    existing_question_hashes.add(get_md5(q.strip()))
            write_log(f"读取现有输出文件，已有 {len(existing_question_hashes)} 条QA")
        except Exception as e:
            write_log(f"⚠️ 读取现有输出文件失败: {str(e)}")
    
    # 写入新增QA对
    new_written_count = 0
    with open(output_file, "a", encoding="utf-8") as f:
        for question, answer in qa_pairs:
            q_stripped = question.strip()
            a_stripped = answer.strip()
            if not q_stripped or not a_stripped:
                continue
            # 去重
            q_hash = get_md5(q_stripped)
            if q_hash in existing_question_hashes:
                continue
            existing_question_hashes.add(q_hash)
            # 写入Markdown格式
            f.write(f"\n## 问题\n{q_stripped}\n\n")
            f.write(f"## 回答\n{a_stripped}\n\n")
            f.write("---\n")
            new_written_count += 1
    
    write_log(f"✅ 写入完成，本次新增 {new_written_count} 条QA到文件: {output_file}")

# ======================================
# 六、主流程（逻辑清晰，一步不落）
# ======================================
def main():
    # 1. 初始化：清空旧日志，创建输出目录
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    write_log("🚀 Cursor对话提取脚本启动")
    
    # 启动就创建输出目录，彻底解决目录不存在的问题
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    write_log(f"📁 输出目录已确保存在: {OUTPUT_DIR}")

    # 2. 权限验证
    cursor_root_dir = os.path.expanduser("~/.cursor/projects")
    if not os.path.exists(cursor_root_dir):
        write_log(f"❌ Cursor项目目录不存在: {cursor_root_dir}")
        sys.exit(1)
    if not os.access(cursor_root_dir, os.R_OK | os.X_OK):
        write_log(f"❌ 无权限读取Cursor目录: {cursor_root_dir}")
        write_log("💡 解决方法：系统设置 → 隐私与安全性 → 完全磁盘访问 → 给终端/IDE开启权限")
        sys.exit(1)
    write_log(f"✅ 权限验证通过，可正常读取Cursor目录")

    # 3. 加载增量状态
    state = load_state()

    # 4. 查找所有对话文件
    transcript_files = find_all_transcript_files()
    if not transcript_files:
        write_log("❌ 未找到任何对话文件，脚本退出")
        sys.exit(0)

    # 5. 逐个处理文件，收集所有有效QA对
    all_valid_qa = []
    for file_path in transcript_files:
        messages, state = read_file_incremental(file_path, state)
        if not messages:
            write_log(f"ℹ️ 文件无新消息，跳过: {os.path.basename(file_path)}")
            continue
        # 合并消息，生成QA对
        qa_pairs = merge_consecutive_messages(messages)
        all_valid_qa.extend(qa_pairs)

    # 6. 写入输出文件
    write_qa_to_markdown(all_valid_qa)

    # 7. 保存增量状态
    save_state(state)

    # 8. 最终结果日志
    write_log(f"🎯 脚本运行完成，本次总新增有效QA数量: {len(all_valid_qa)}")

if __name__ == "__main__":
    main()