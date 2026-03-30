# Personal-AI-skills

## cursor-conversations-review

`cursor-conversations-review` 用于对最近一段时间的 Cursor 对话进行结构化复盘。

### 作用
- 通过脚本增量提取本机 Cursor 历史对话内容
- 生成当天复盘输入文件：`cursor-conversations-review/reviews/yyyy-MM-dd.md`
- 基于对话内容进行复盘分析，输出：
  - Session 分类
  - 提问质量分析（用户侧）
  - 回答有效性分析（模型侧）
  - 可复用优化建议

### 如何触发

- 将该 skill 添加到 Cursor 后，在 Cursor 对话中提出复盘诉求即可触发该 Skill（如包含“对话复盘 / 回顾总结 / session 分析”等意图）：
- 帮我复盘一下最近的对话
- 总结一下我这段时间怎么和你交流的
- 对最近会话做一次 session 分析并给出优化建议
