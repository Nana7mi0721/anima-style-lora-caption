# anima-style-lora-caption

一个 Agent Skill，指导 agent 为 [Anima](https://huggingface.co/circlestone-labs/Anima) LoRA 数据集执行**打标 / 洗标**工作。

它负责「怎么把这件事做完」：探测环境与信息源、下采样、生成标签动作表、派发看图子代理、解析合并、规范化、校验写回。

**标签该写什么内容**（格式、槽位、该删该留、规范化标准）的权威定义在 `references/Anima_style_LoRA_打标指南.md`。

## 适用场景

- 给一批图片生成 LoRA 训练用的 caption
- 清洗/规范化已有的 caption（别名、废弃标签、否定式、形态不一致）
- 整合多个信息源（booru 权威标签 / 图片本身 / 自动打标初稿）产出最终标签串
- 需要看图补全的字段（人数、构图、体位细节等）

## 安装

把本仓库放到你的 skill 目录下：

```
<skills-dir>/anima-style-lora-caption/
├── SKILL.md
├── assets/
│   ├── workflow.md
│   └── normalization.md
└── references/
    └── Anima_style_LoRA_打标指南.md
```

核心要求是 `SKILL.md` 位于 skill 根目录，且其 YAML frontmatter 中的 `name` / `description` 被框架识别。

## 两条铁律

1. **主代理永不读图** —— 看图一律交子代理，主代理只处理文本与文件，避免图片进入主上下文打断长会话
2. **写一张落一张** —— 子代理每处理完一张立刻写盘，会话中断时只丢最后几张

## 执行流程（七阶段）

```
阶段 -1  归档现有初稿（写回会覆盖同名文件，必须先留档）
阶段  1  下采样 → 缩略图
阶段 1.5 生成 tag 动作表
阶段  2  派子代理看图 → 结构化槽位标注
阶段 2.5 解析 → 结构化 JSON + 失败台账 + 复核清单
阶段  3  合并：三源结合 + 归一化 + 槽位排序
阶段  4  校验：格式闸门 + 标签有效性闸门
阶段  5  备份 → 写回 → 抽查
```

## 三条最关键的实战经验

1. **提示词必须是「结构化槽位填空」，不能是开放式描述** —— 字段名列表把任务锚定成「填表」，开放式描述会触发内容审核拒绝。这是能否跑完的决定性因素。
2. **失败只记台账，不兜底不重试** —— 「返回空就退回纯文本标注」会把失败藏起来。
3. **整批拒答 → 缩小派发粒度** —— 拒答往往是「批次级」的，改成 1 图 1 代理比改措辞有效得多。

## 目录结构

```
SKILL.md                       工作流总纲（七阶段 + 分流 + 红线）
assets/workflow.md             执行详规（下采样/派发/提示词模板/裁切纪律/失败处理/校验写回）
assets/normalization.md        标签规范化对照表（别名/废弃/否定式/白名单）与词典校验
references/
  Anima_style_LoRA_打标指南.md  参考文档：标签内容规则
```

## 配套 skill

训练配置生成见 **anima-lora-config**（含《Anima LoRA 调参指南》）。

## 许可

MIT，见 [LICENSE](LICENSE)。
