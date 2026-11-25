# Trainer 源码核实、排障诊断与参考资料

> 本文件为 [Anima_style_LoRA_打标指南.md](./Anima_style_LoRA_打标指南.md) 的按需扩展参考，在出现训练报错、效果纠缠或流水线异常时查阅。执行层面中断恢复见 [workflow.md](../assets/workflow.md)。

---

## 1. Trainer 侧源码核实与 SDXL 经验对照

### 1.1 Anima-Standalone-Trainer 源码事实

| 行为 | 源码定位与实现 | 对 Caption 的硬约束 |
|---|---|---|
| **只读首行** | `library/train_util.py:872` `caption = caption.split("\n")[0]` | 第二行起静默丢弃且无警告，必须严格单行 |
| **`shuffle_caption` 互斥** | `anima_train_network.py:63` 断言开启 `cache_text_encoder_outputs` 时禁止 `shuffle_caption` / `token_warmup_step` / `caption_tag_dropout_rate` | 标准双缓存配方下 `shuffle_caption` 必须为 `false`，否则训练直接报错中止 |
| **按 `,` 切分** | `library/train_util.py:4517` `caption_separator` 默认 `","` | 逗号是唯一 token 边界；混排 NL 句内含逗号会被切碎 |
| **subset dropout 上提** | `anima_train_network.py:48-59` `Migrating subset caption dropout rate … to global level for Anima strategy` | subset 级 dropout 取全局最大并迁移到 embedding 级以兼容 TE 缓存，不同于 SDXL 的逐 batch 随机丢弃 |
| **Token 上限** | `library/anima_train_utils.py:101,107` `--qwen3_max_token_length` / `--t5_max_token_length` | 默认各 512；20~45 标签约 40~70 token，远未触顶 |
| **稳定锚点** | `library/train_util.py:4515-4538` `keep_tokens` / `shuffle` 定义 | 关闭 shuffle 后，**触发词置首 + `keep_tokens=1`** 保持锚点稳定 |

### 1.2 不要照抄 SDXL 打标经验

| SDXL 常见做法 | Anima 实际情况 |
|---|---|
| 「开 shuffle 防顺序绑定」 | **不可用**（与 TE 缓存硬冲突），靠触发词置首 + embedding 级 caption dropout |
| 全集统一加 `masterpiece, best quality` | **禁止**。质量词仅在逐张测量时有效，全集常量只会制造噪声 |
| 保留 `absurdres / highres / anno` | **删除**。属来源 meta，非画面视觉特征 |
| 自然语言越长越详细越好 | 标签精度优先；实测纯净纯标签串（均值 30~39 词）效果最稳 |
| 堆叠多层级 tagger 标签 | Anima 训练期含随机 tag dropout，冗余父标签只会稀释核心信号 |

---

## 2. 症状 → 第一检查矩阵

| 现象 | 第一检查项 |
|---|---|
| caption 只有前半截生效 | 文件出现换行，Trainer 只取首行 |
| 报错 `shuffle_caption, token_warmup_step or caption_tag_dropout_rate cannot be used` | 关闭 `shuffle_caption`（双缓存模式下强制要求） |
| 触发词不生效或很弱 | 画风 LoRA 是否遗漏 `@` 前缀、触发词是否置首、`keep_tokens` 是否设为 `1` |
| 风格与固定角色/构图绑死 | 数据集多样性不足、未写变化因素、或常驻角色 tag 去留不当 |
| 某特征（发色/服装）推理改不动 | 检查该特征是否在训练集 caption 中被正确写出（写了才可控） |
| caption 出现怪词或反向词 | 初稿幻觉、口语形或杜撰否定式，检查是否经 [parse_descriptions.py](../scripts/parse_descriptions.py) 与 [rules/vision_fix.json](../rules/vision_fix.json) |
| 「自造控制词无效」 | 查 Danbooru posts 数——为 0 即噪声（如 `left side ponytail` 教训） |
| 同义词两套写法并存 | 检查是否混用下划线/空格形（跑 [validate_captions.py](../scripts/validate_captions.py) 离线闸门） |
| 成品带出训练水印 | 数据集未去净的水印未标 `watermark` 进槽位 7，被模型学成画风 |
| 多角色图全标成 `1girl` | 未看图直接采用残缺文本兜底，需重派视觉槽位补齐 |
| 修改白名单/归一化后依然缺词 | 只重跑了合并未重跑解析——改动 `rules/whitelist.txt` 或 `rules/vision_fix.json` 后**必须先重跑解析再合并** |
| 在线校验全报未知 | 检查是否触发代理/网络失败导致 `UNVERIFIED`（用 `--proxy` / `--use-curl`），不可误判为 `INVALID` |

---

## 3. 打标侧排障处方链（按序排查，一次一步）

```text
① 数据质量（模糊、重复、差分一致性、可去除水印是否先清理）
→ ② 变化因素是否逐张写出（漏写 = 被吸收进触发词）
→ ③ 固有特征取舍是否符合目标类型（角色 vs 画风，见 target_types.md）
→ ④ 标签有效性与形态一致性（废弃形/别名/全小写空格形/无下划线）
→ ⑤ 是否过标或欠标（20~45 词不含触发词；超限用 trim_tiers）
→ ⑥ 质量词、IP 系列名与无视觉对应物 meta 是否清零
→ ⑦ 触发词覆盖率、首位对齐与 keep_tokens=1
```

---

## 4. 风格-内容纠缠专项手册

- **典型症状**：激活画风 LoRA 后，男性/老人长出少女脸、白天场景自动变霓虹夜景、所有出图复用训练集固定构图或角色模板。
- **根本原因**：某些内容特征与触发词在数据集中**高度共现**（例如 90% 样本为白发少女，导致 `white hair` 与 `@artist` 绑定）。
- **处方（按优先级依次执行）**：
  1. **补齐反例数据**：加入男性、老人、动物、建筑、机械、纯风景，打破共现统计。
  2. **Caption 充分解耦**：把发色、服装、光影、构图全部如实写出，不留盲区。
  3. **清理近重复构图**：删除缩放/裁剪重复图，拆散视觉近重复组。
  4. **调整训练步数与学习率**：参见 `Anima_LoRA_调参指南.md` 或 `anima-lora-config` skill。
  5. **最后才考虑调高 dropout**：前四步未做前勿盲目依赖 dropout。

---

## 5. 外部事实来源与文献索引

**官方与社区资料**：
- [circlestone-labs/Anima 模型卡](https://huggingface.co/circlestone-labs/Anima)：官方标签段顺序、`@` 画师前缀要求、全小写空格规范、质量词与混合用法。
- [Anima 讨论区 #205 Training Tagging Specifications](https://huggingface.co/circlestone-labs/Anima/discussions/205) / [#96 Strategy for captioning](https://huggingface.co/circlestone-labs/Anima/discussions/96)：社区打标实操共识。
- [lora-training-skill caption-guide](https://github.com/Rinne414/lora-training-skill/blob/main/references/caption-guide.md)：Trainer 源码核实、Caption 悖论、WD14 阈值、≥0.9 剪切与父标签折叠。
- [AnimaLoraStudio 中文文档](https://github.com/WalkingMeatAxolotl/AnimaLoraStudio)：端到端打标流水线参考。
- [WAI-Anima caption rework 记录](https://lilting.ch/en/articles/wai-anima-lora-caption-rework)：`left side ponytail` 零帖教训与空间信息交由自然语言的实践。
- [Civitai: Beginner's Guide to Local Anima LoRA Training](https://civitai.com/articles/31678/a-complete-beginners-guide-to-local-anima-lora-training)：`@` 触发词、`keep_tokens=1` 与过标/欠标判断。
- [Civitai: The Ultimate LoRA Tagging Guide](https://civitai.com/articles/29347/the-ultimate-lora-tagging-guide-from-dirty-image-to-perfect-model)：仅标可见物、禁否定式、同义词统一。
- [danbooru-tag-expander](https://github.com/allenday/danbooru-tag-expander)：有向别名（antecedent → consequent）判定现行形与废弃形。
