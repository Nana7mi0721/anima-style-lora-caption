# Anima LoRA 打标指南（通用版）

> 本篇只讲**标签该写什么内容**——格式、槽位、取舍、规范化与校验标准。
> **执行层面**（怎么探测环境、怎么派发看图子代理、怎么下采样解析合并写回）见 `anima-style-lora-caption` skill。
> 参数怎么设定见 `Anima_LoRA_调参指南.md`。
>
> 关键格式约束已对照 Anima-Standalone-Trainer 源码（`library/train_util.py`、`anima_train_network.py`）与 Anima 官方模型卡核实，
> 并结合多个实际数据集的洗标成果与社区/官方文档统合。版本：2026-09

---

## 目录

1. [基础事实：Anima 吃什么 Caption](#一基础事实anima-吃什么-caption)
2. [核心心智模型：该标什么、不该标什么](#二核心心智模型该标什么不该标什么)
3. [Caption 格式硬约束](#三caption-格式硬约束)
4. [槽位结构与顺序](#四槽位结构与顺序)
5. [按训练目标的打标差异](#五按训练目标的打标差异)
6. [标签来源与三源优先级](#六标签来源与三源优先级)
7. [标签规范化：现行形、别名、废弃、否定式](#七标签规范化现行形别名废弃否定式)
8. [专项条款](#八专项条款)
9. [校验标准](#九校验标准)
10. [排障诊断](#十排障诊断)
11. [附录](#十一附录)

---

# 一、基础事实：Anima 吃什么 Caption

## 1.1 官方标签顺序（模型卡事实）

Anima 官方模型卡规定的标签段顺序：

```
[quality/meta/year/safety] [1girl/1boy/1other] [character] [series] [artist] [general tags]
```

- 全部逗号分隔；**段内顺序自由**，段间顺序固定
- **全小写**，**空格而非下划线**（唯一例外是 `score_7` 这类评分标签，其下划线是本体）
- **画师标签必须加 `@` 前缀**——模型卡原文：「不加 `@` 效果会非常弱」
- 训练数据混合了「纯标签 / 纯自然语言 / 两者混合」，且做了随机 tag dropout ⇒ **不要求写全所有相关标签**

## 1.2 安全/评级词汇是 Anima 自己的，不是 danbooru 的

| danbooru rating | Anima 对应词 |
|---|---|
| general | `safe` |
| sensitive | `sensitive` |
| questionable | `nsfw`（danbooru 没有这个词） |
| explicit | `explicit` |

规则：评级**只写一次**，放在 `[safety]` 段，**并且从 general 标签串里剔除**原始 rating 词。一条 caption 里出现两个互相矛盾的评级是最典型的错误；角色 LoRA 全部不写评级也是可接受的，但**不能把裸的 `questionable`/`general` 留在标签体里**。

## 1.3 Trainer 侧的硬行为（源码核实，决定格式红线）

| 行为 | 源码事实 | 后果 |
|---|---|---|
| **只读第一行** | `train_util.py:872` `caption = caption.split("\n")[0]` | 多行 caption 的第二行起**静默丢弃**，无警告。必须单行 |
| **`shuffle_caption` 不可用** | `anima_train_network.py:63` 断言：开启 `cache_text_encoder_outputs` 时 `shuffle_caption` / `token_warmup_step` / `caption_tag_dropout_rate` 都不能用 | Anima 标准配方（双缓存全开）下 **shuffle 必须 false**，写 true 直接中止训练 |
| **caption 按 `,` 切分** | `caption_separator` 默认 `","`（`train_util.py:4517`） | 逗号是唯一的 token 边界；自然语言句里的逗号会被当标签切碎（本指南默认不 shuffle，影响有限，但混排 NL 时要注意） |
| **subset caption dropout 被上提** | `anima_train_network.py:48-59` 日志 `Migrating subset caption dropout rate … to global level for Anima strategy` | subset 级 `caption_dropout_rate` 取全局最大值并**迁移到 embedding 级**，与 TE 缓存兼容。它不是 SDXL 式逐 batch 随机丢 caption |
| **token 上限** | `--qwen3_max_token_length` / `--t5_max_token_length`（`anima_train_utils.py:101,107`） | 默认各 512。成品 25~40 标签（约 40~70 token）远未触顶，**不要写长篇散文** |

> **结论**：Anima 的 caption 是**单行、逗号分隔、冻结顺序**的标签串；shuffle 关掉后「标签顺序绑定」不再是主要风险，但**触发词前置 + `keep_tokens=1`** 仍应保留，作为稳定锚点。

## 1.4 与 SDXL 通用打标经验的差异（不要照抄）

| SDXL 教程常见做法 | Anima 上的实际情况 |
|---|---|
| 「开 shuffle 防顺序绑定」 | **不可用**。与 TE 缓存硬性冲突。防绑定靠触发词前置 + caption dropout |
| 每张都写 `masterpiece, best quality` | **不要**。质量词只在**逐张测量得出**时才是有效的条件标签；整个数据集盖同一个词 = 制造噪声 |
| `absurdres / highres / anno` | 删除。这类是来源 meta，不是画面特征 |
| 自然语言越详细越好 | Anima 支持混合，但**标签精度优先**；实测成品是纯标签串，均值 30~39 词 |
| tagger 风格多标签堆叠 | Anima 有随机 tag dropout，堆量无收益；**去掉冗余泛化父标签**（见 7.4） |

---

# 二、核心心智模型：该标什么、不该标什么

## 2.1 第一原则（Caption 悖论）

> **你想在未来用 prompt 独立控制的东西 → 写进 caption；
> 你想让 LoRA（触发词）自己吸收的东西 → 不写。**

这是全部打标决策的总纲。由它派生出三条例：

1. **变化因素必须逐张写**（姿势/服装/表情/场景/镜头/光照）。漏写 = 模型把它当固有特征吸收进触发词。
2. **固有特征按目标类型决定写不写**。角色 LoRA 的发色瞳色可写可不写（写了可控、不写则并入触发词）；画风 LoRA 的「风格」本身绝对不能写。
3. **不在画面里的东西不写**。看不到就留空，**绝不写否定式**（见 7.5）。

## 2.2 过标与欠标

| 错误 | 表现 | 后果 |
|---|---|---|
| **过标** | 每张 60+ 标签、把 `mammal`/`animal`/`fur` 重复堆叠、写泛化父标签 | 稀释真正要学的概念；用户不会在 prompt 里打这些词 |
| **欠标** | 只有 `@trigger, solo, sitting` | 模型被迫猜；数据集里恒定出现的衣服/表情/背景被当成角色的一部分 |

**判断口诀**：如果删掉这个标签不会改变别人怎么用你的 LoRA，就质疑它该不该在。

**实测标签数（可作为区间参考）**：

| 数据集类型 | 张数 | 均值 | 中位 | 最小 | 最大 |
|---|---:|---:|---:|---:|---:|
| 画风 A | 253 | 30.8 | 30 | 17 | 61 |
| 画师风格 B | 250 | 31.1 | 29 | 13 | 62 |
| 画师风格 C | 81 | 38.6 | 39 | 25 | 51 |

⇒ **实用区间 20~45 词**；社区口径 5~40 与之吻合。低于 15 词基本是欠标，高于 60 词要逐个质疑。

## 2.3 该删的（不是画面特征）

| 类别 | 例子 | 理由 |
|---|---|---|
| **画师/社团/公司标签** | `someartist_(handle)`、`some_circle`、`some_shop` | 由触发词承载 |
| **IP / 版权系列名** | `touhou`、`blue_archive`、`love_live!` | **永不添加**。曾一次性清理 53 个 IP 系列名 |
| **meta / 媒介 / 质量** | `uncensored`、`highres`、`absurdres`、`photoshop`、`digital_version`、`artist_revision`、`translated`、`detexted`、`wallpaper` | 无对应视觉事实的常量 |
| **低频角色** | 见 8.2 阈值规则 | 学不会，只把风格打散成噪声 |
| **文字族（已废弃）** | `text` / `japanese text` / `english text` / `chinese text` | danbooru 已废弃整族，改由 `speech bubble` / `sound effects` / `logo` 承载 |
| **否定式** | `no cum`、`tongue not out`、`no penetration`、`without clothing` | 反向标签，比漏标更糟 |
| **纯散文句** | `looking at the scene`、`two girls arranged horizontally in frame` | 不是可学的标签 |
| **模型自造词** | `very aroused`、`detailed eyes`、`light blue hair`（词典无） | 底模没见过，等于噪声；一律换成现行标签 |

## 2.4 该保留的（即使是常量）

**判断标准不是「出现频率」，而是「有没有固定视觉对应物」**：

- `uncensored`(226/250) 删 —— **没有像素对应**，纯 meta
- `watermark`(123/123 恒定) 保留 —— **有固定像素对应**。标了，模型才把那片像素归因到 `watermark` 而不是画风；不标，水印必然被固化成风格的一部分
- `wet`(144/250) 保留 —— 视觉事实，且正是该画师的风格特征
- `nipples`(223/250) 保留 —— 身体事实（性与身体描写**不做道德性删减**）

---

# 三、Caption 格式硬约束

**违反即训练异常或静默降级**，写完后必须逐条机检。

| # | 约束 | 依据 |
|---|---|---|
| 1 | **单行**。绝不换行 | `train_util.py:872` 只取第一行 |
| 2 | **全小写** | 官方模型卡 |
| 3 | **空格，不用下划线**（`score_*` 例外） | 官方模型卡 |
| 4 | **分隔符 = 半角 `, `**（逗号+空格） | 与全角逗号混用会切分失败 |
| 5 | **UTF-8 无 BOM、无尾换行** | 校验器实测要求 |
| 6 | **无前导逗号** | 自动打标工具常留 `{触发词}, {输出}` 模板残留，必须先剥 |
| 7 | **触发词置首且唯一 `@`** | `@` 只允许出现在首位触发词（角色名带 `@` 时例外需登记） |
| 8 | **无重复 token** | 重复标签稀释训练信号 |
| 9 | **无否定式** | 反向标签 |
| 10 | **无质量词**（除非逐张测量） | 官方：不要给整个数据集盖同一个词 |
| 11 | **一个数据集内形态统一** | 同一事实只能有一种写法 |
| 12 | **标签数 20~45** | 实测成品均值区间 |

## 3.1 混排自然语言时（可选，非默认）

实测成品是**纯标签串**。若确要写自然语言（Anima 支持混合）：

- 标签段与散文之间用 **`. `**（句点+空格）过渡，这是官方模型卡演示的形态：
  `masterpiece, best quality, @big chungus. An anime girl with medium-length blonde hair is...`
- 散文写 **1~3 句**，且**仍必须在同一行**
- 散文**不得复述标签**，只写标签表达不了的东西：空间布局、光照方向、材质氛围
- ⚠️ `shuffle_caption` 恒为 false，所以散文不会被切碎；但一旦开了 shuffle，含逗号的散文句会被撕成伪标签

## 3.2 形态不一致的真实案例（引以为戒）

曾出现同一数据集内两种形态并存：

```
0001: @qianhen, 1girl, solo, black_hair, very_long_hair, ...   ← 下划线形
0003: @qianhen, 1girl, solo, black hair, very long hair, ...   ← 空格形
```

同一数据集两种写法 = 同一个概念被拆成两套 token，**等于把数据量减半**。写完后必须全量检查。

---

# 四、槽位结构与顺序

## 4.1 七槽位（通用结构）

```
@触发词, [1 主体] 人数 + 关系 + 角色名
→ [2 外观着装] 发色/发型/发长/瞳色/表情/视线/身材/服装/饰品/身体特征
→ [3 姿势动作] 姿势/体位/手部/性描写动作/生殖器/体液
→ [4 场景环境] 地点/道具/背景
→ [5 光影色调] day/night/sunlight/warm tone/cool tone/dark…
→ [6 构图镜头] upper body/cowboy shot/from above/close-up/full body…
→ [7 水印区] watermark / 颜色词 / signature / logo（无则整区省略）
```

**槽位编号仅用于内部描述，实际输出没有槽位标记。**

## 4.2 槽位 2 / 3 的分界（易错，已校正）

| 归槽位 2 | 归槽位 3 |
|---|---|
| 发/瞳/**表情**/**视线**/体型/服装/身体特征（`nipples` `breasts` `nude` `breasts out` `panties`） | 姿势/体位/手部动作/性行为/生殖器/体液（`pussy` `penis` `anus` `ass` `cum` `spread legs` `on back`） |

> 旧规则曾把「视线」放槽位 3，与多个成品集的惯例都不符，**已更正为归槽位 2**。

## 4.3 与官方顺序的对应

| 官方段 | 本结构槽位 |
|---|---|
| `[quality/meta/year/safety]` | 一般**不写** |
| `[count]` | 槽位 1 的人数部分 |
| `[character]` / `[series]` | 槽位 1 的角色部分（**角色 LoRA 不写**，见 5.2） |
| `[artist]` | 触发词（`@` 前缀） |
| `[general tags]` | 槽位 2~7 |

**与官方顺序的差异是刻意的**：把触发词置首而非放在 character 段，配合 `keep_tokens=1` 形成稳定锚点。两者都合法，**关键是全数据集统一**。

## 4.4 各槽位的信息主来源（决定看图要重点问什么）

不同来源对各字段的覆盖差异很大（以一个 250 张三源数据集实测为例）：

| 槽位 | booru 权威标签 | 自动打标初稿 | 看图 |
|---|---|---|---|
| 1 主体 | 角色名单 | ❌ 人数仅 25% | 🔴 **人数+关系必须看图给** |
| 2 外观着装 | 服装/身体状态 | 发色 81% / 瞳色 75% / 服装 64% | 校验初稿 + 补漏 |
| 3 姿势动作 | 性行为**大类** | 姿势 87% | 🔴 **体位/行为细节必补** |
| 4 场景环境 | 极稀疏 | ✅ **99%（主来源）** | 校验初稿 |
| 5 光影色调 | **0 个** | ✅ **96%（主来源）** | 校验初稿 |
| 6 构图镜头 | **0 个** | ❌ **7%（几乎无）** | 🔴 **必须看图给** |

⇒ **看图环节的「必答字段」必须锁定在权威标签/初稿覆盖最差的地方**：人数、关系、构图、体位细节。否则等于让看图者重做已经做完的事。

---

# 五、按训练目标的打标差异

## 5.1 总表

| 维度 | 人物/角色 | 画师风格 | 服装 | 物体/概念 | 场景/环境 |
|---|---|---|---|---|---|
| **角色 tag** | 写（客串不写） | 通常不写 | 写 | — | — |
| **固有特征**（发色瞳色） | 可写可不写（见 5.2） | 不写「风格」，内容正常写 | 逐张写 | 逐张写 | 逐张写 |
| **触发词 `@`** | 可 `@` 或裸词 | **必须 `@`** | 可 `@` | 可 `@` | 可 `@` |
| **变化因素** | 姿势/服装/表情/背景**必写** | 全部内容必写 | 角色/服装/姿势/环境四层**必须解耦** | 状态/朝向/使用中 | 构图/远近/时间/天气 |
| **质量词** | 不写 | 不写 | 不写 | 不写 | 不写 |
| **数据多样性** | 中 | **极高（第一要求）** | 高 | 中 | 高 |

## 5.2 人物/角色 LoRA

- **角色名 tag 与触发词二选一**：一旦触发词承载身份，就删掉 `furina_(genshin_impact)` 这种角色名标签（多个角色 LoRA 成品集均无角色名 tag）
- **客串角色不擅自定名**：出现 1~2 次的别的角色，**人数照实写（`2girls`），身份不写**；若反复出现（≥4 次）先出频次表交用户裁决
- **固有特征的取舍**（核心决策）：
  - **写**：发色/瞳色变成可调项，灵活性高，适合新手
  - **不写**：让触发词吸收，用法更简单，但无法改发色
  - 进阶：**只剪真正不变的特征**——判据是出现比例 **≥0.9**，一次性的 `wet hair` 绝不能剪
- **表情/视线/姿势/背景必须逐张写**——这些恰恰是用户最想控制的东西

## 5.3 画师风格 LoRA

- **`@` 前缀必用**（官方模型卡原文：不加效果很弱）
- **绝不描述「风格」本身**：不要写 `thick outlines`、`low saturation`、`beautiful eyes`——**让权重去学**，这才是风格 LoRA 的目的
- **审美词慎用**：`masterpiece`/`best quality` 与触发词强共现时会纠缠
- 内容要**解耦**：不同角色、不同题材、不同构图都写清楚，让风格成为「唯一没被解释掉的信号」
- 角色 tag：画师作品的常驻角色**几乎等于第二个触发词**（曾有数据集某角色 113/123 恒在）。保留能分流角色特征、风险是与画风强耦合——**默认写，去留交用户裁决**
- 验收标准不是「风格最强」，而是**陌生内容上仍保持风格且不污染内容语义**（男性/动物/建筑/机械/风景）

## 5.4 服装 LoRA

Caption 必须清楚区分 **角色 / 服装 / 姿势 / 环境** 四层，否则「这个角色穿这衣服」会学成「这角色 = 这衣服」。**覆盖不同角色穿同一服装的数据最有价值。**

## 5.5 物体 / 概念 / 场景

- 物体：写**朝向与状态**（`open`/`closed`/`folded`/`worn`/`in use`）——这是最常见的纠缠来源
- 场景：**构图多样（远/中/近景、横/竖构图）是数据集第一要求**；写空间关系与时间/天气
- 复杂设计语言（机甲体系/建筑风格）按画风 LoRA 路线处理

---

# 六、标签来源与三源优先级

## 6.1 三个源

| 代号 | 来源 | 性质 |
|---|---|---|
| **A** | booru 权威标签 | **事实基准**（有 A 时不可被推翻） |
| **B** | 图片本身（看图） | 真相 |
| **C** | 自动打标初稿（JoyCaption / WD14 / VLM） | **待校验的输入，不是成品** |

## 6.2 优先级

```
有 A：  A 已覆盖的字段 → 直接采用，B/C 不得推翻
        A 未覆盖的字段 → B 看图补 + C 校验后填
        C 与 A 冲突      → 丢弃 C，采用 A
        C 与 B 不符      → 判 C 幻觉，丢弃或纠正
        A/B/C 都没有     → 留空，不编

无 A：  B 为骨（视觉产出即 caption 主体），C 只做交叉核对
        B/C 冲突 → 判 C 幻觉，丢弃
```

## 6.3 自动打标初稿的已知坑（C 源）

| 坑 | 实测 |
|---|---|
| **前导 `, `** | 多个数据集 100% 中招（`{触发词}, {输出}` 模板残留），处理前必须剥 |
| **幻觉** | 会自信地标出画面里没有的东西（`necklace`/`wolf`/`open mouth`）；曾出现原文本与图**完全错位**（旧文本描述金发柠檬背景，实图为黑短发紫眼白裙） |
| **人数覆盖差** | 仅 25%；构图仅 7% |
| **服装漏标** | 实测约 1/3 缺失，是纠错重点 |
| **描述的是处理前的原图** | 若图片做过去字/裁剪，初稿可能仍在描述已不存在的内容，必须丢弃 |
| **否定式倾向** | 会写 `no cum` / `tongue not out` / `no penetration` 来表达「没看到」——**这是反向标签** |
| **口语/空标签** | `eyes closed`、`mouth open`、`twin tails`、`from front`、`X visible`、`slightly X` |

⇒ **绝不要直接信任自动打标结果**。实测首轮产出的唯一标签里可有近三成不合规。

## 6.4 用 WD14 类 tagger 时的参数

| 项 | 建议 | 说明 |
|---|---|---|
| 阈值 | 按模型取：`wd-eva02-large-tagger-v3` ≈ **0.53**；`wd-swinv2-tagger-v3` ≈ **0.27** | 模型卡发布的 P=R 点只是起点，**不是通用最优** |
| 角色阈值 | **保守取 0.85** | 角色标签标错代价最高（教错身份） |
| 标定方法 | 选 10~20 张有代表性的图 → 数「有用标签 / 错标 / 漏标」→ **只调一次阈值** → 再抽查 | 不要一次上全量 |
| 输出形态 | 空格而非下划线 | 与 Anima 官方一致 |
| 后处理 | 必须过一遍规范化 | 原始输出是**置信度降序**，会打乱官方段顺序 |

> **置信度序 ≠ 段序**：WD14 原生 dump 会把 character/series/artist 标签散落在 general 里，违反官方段顺序，必须重排。

---

# 七、标签规范化：现行形、别名、废弃、否定式

标签规范化是**合并与校验阶段自动执行**的步骤（具体操作方法见 `anima-style-lora-caption` skill 的 `assets/normalization.md`）。本章给出**规则与对照表**。

## 7.1 词典是前提

- 用**刷新版** danbooru 词典
- 🔴 **拉取必须按使用量降序**：danbooru `tags.json` 默认按 **id 倒序**（最新标签在前），不带排序参数会一路抓刚创建的长尾标签，**抓几十万行也抓不到 `nude` / `1girl`**
- 🔴 **旧词典可能残缺**：实测有词典缺 `naked`（某数据集最高频）、`breast_grab`、`tan_lines`、`swimsuits`、`bunny_ears` 等有效标签。拿残缺词典当硬闸会把最高频标签判成未知
- **校验用「转换前的下划线原形」**：词典是下划线格式（实测 `katou_megumi` 命中、`katou megumi` 不命中）⇒ 校验通过后再做空格化
- **`is_deprecated=1` 的必须拦下**（如 `topless`、`ass_grab`、`breast_hold`）
- **真正的闸门是在线校验**：`search[name_comma]` 批量查询（每批 120 个），权威且最新

## 7.2 别名 / 废弃 → 现行形（实测对照表）

| 原词 | 现行形 | 依据 |
|---|---|---|
| `pantsu` | `panties` | 别名 |
| `seifuku` | `serafuku` | 别名 |
| `megane` | `glasses` | 别名 |
| `naked` | `nude` | 别名 |
| `topless` | `breasts out` | **已废弃无别名**，换具名等价标签 |
| `ass_grab` | `grabbing another's ass` | **已废弃无别名** |
| `breast_grab` | `grabbing another's breast` | 别名 |
| `breast_hold` | `grabbing own breast` | **已废弃无别名** |
| `tan_lines` | `tanlines` | 别名 |
| `bunny_ears` | `rabbit ears` | 别名 |
| `swimsuits` | `swimsuit` | 别名 |
| `bikini_top` | `bikini top only` | 别名 |
| `areola` / `areolae` | **删除** | 两者均废弃且无别名（posts=0），信息由 `nipples` 承载 |
| `semen` | `cum` | 已废弃 |
| `bikini_pull` | `clothes pull` + `one breast out` | 已废弃，拆成两个现行标签 |
| `front view` / `back view` / `side view` | `straight-on` / `from behind` / `from side` | 前者 danbooru **posts=0** |
| `looking away` | `looking to the side` | 已废弃 |
| `cowgirl` | `cowgirl position` | ⚠️ `cowgirl` 有 active alias 指向 `cow girl`（牛仔女郎**服装**）；若语境是**体位**，必须显式覆盖 |
| `smiling` | `smile` | 非 danbooru 词 |
| `slender` | `skinny` | 视觉层实测 |
| `group shot` | `group picture` | 视觉层实测 |
| `on all fours` | `all fours` | 视觉层实测 |
| `hair clip` | `hairclip` | 视觉层实测 |
| `hairy arms` | `arm hair` | 视觉层实测 |
| `reverse cowgirl` | `reverse cowgirl position` | 视觉层实测 |
| `bangs` | **删除** | 已废弃 |
| `tights` | `pantyhose` | 词典有 pantyhose |
| `sofa` | `couch` | 词典有 couch 无 sofa |
| `indoor background` / `outdoor background` | `indoors` / `outdoors` | 实测 |
| `low angle` / `high angle` | `from below` / `from above` | 实测 |
| `thigh-highs` | `thighhighs` | 实测 |
| `hand on face/cheek` | `hand on own face` / `hand on own cheek` | 实测 |
| `nopan` | `no panties` | 实测 |
| `shimapan` | `striped panties` | 实测 |

> **吃 alias 前先确认语义**：别名表只保证词形合法，不保证语义正确（`cowgirl` 就是反例）。

## 7.3 颜色与复合形

- **词典无的复合色一律降级为泛色**：`light blue hair`→`blue hair`、`silver hair`→`white hair`、`golden/amber eyes`→`yellow eyes`
- **同图 ≥2 种发色** → 加 `multicolored hair`
- **并存同类**（`dress` + `white dress`、`bow` + `blue bow`）→ **保留具体形，删泛形**

## 7.4 折叠蕴含父标签

有具体标签时删掉泛化父标签：有 `cat ears` 就删 `animal_ears`；有 `school swimsuit` 就删 `swimsuit`。泛化父标签是纯冗余。

> ⚠️ 例外：若父标签是**故意留白**的（例如让视觉层的具体种属字段独占，因为权威标签的标注不可靠），要在动作表的 `note` 里说明，否则复查时会当成漏删。

## 7.5 否定式：**零容忍**

- 自动打标/看图环节会用 `no cum` / `tongue not out` / `no penetration` / `no male visible` / `without clothing` 表达「没看到」
- **两层防护**（缺一不可）：
  1. **提示词层**：写明「看不到就写 `none`，不要写否定式」
  2. **合并层兜底**：`^(no|without|not)\b` 一律丢弃 —— **但 `no panties` / `no bra` 本身就是标签，必须保留**
- 实测最终残留应为 **0**

## 7.6 描述词白名单（非 danbooru 但有用的光影/构图词）

danbooru 没有对应的光影/构图标签，但成品集在用，且**对画风 LoRA 是可学的视觉信号**：

| 类别 | 词 |
|---|---|
| 光影 | `warm tone` `cool tone` `soft lighting` `soft light` `bright lighting` `natural lighting` `warm lighting` `dim lighting` `cinematic lighting` `soft shadows` `side lighting` `strong light` `strong shadows` `hard shadow pattern` `low contrast` `cool-warm mixed` `bright indoor light` `sunlight from left` `sunlight through blinds` `soft body highlight` |
| 构图 | `diagonal composition` `multi-figure composition` `eye level` `foreground focus on hips` |
| 水印颜色 | `blue watermark` / `red watermark` |

**收录标准**：语义明确、可复现、不与已有词高度重复。**纯散文句仍丢弃**（`looking at the scene`、`two girls arranged horizontally in frame`）。

## 7.7 「不存在的标签」= 噪声（重要教训）

实测案例：为控制侧马尾的左右方向写了 `left side ponytail`——**danbooru 上 posts=0**。从底模视角，它只是 `side ponytail` + 噪声，**方向信息从未被传达**。

现行形只有：`side ponytail`（父）、`high/low side ponytail`、`short side ponytail`、`side drill`。

**规则**：
1. 标签侧**只用真实存在的标签**（`left side ponytail` → `side ponytail`）
2. 表达不了的**方向/空间信息交给自然语言**（"visible on the right side of the image"）
3. 凡「造一个标签来精确控制」的冲动，**先在词典里查 posts 数**——为 0 就是白写

---

# 八、专项条款

## 8.1 水印 / 签名 / 残留文字

**决策树**：

```
能在图像层去掉/裁掉？
  ├─ 能 → 去掉（比标出来更好，直接消灭问题）
  └─ 不能（去不掉、裁掉会破坏构图、水印满图都是）
        └─ 有固定视觉对应物 → 标出来
              ├─ 恒定出现 → 每张都标 watermark
              └─ 逐张出现 → 按视觉事实标，无则整区省略
```

**标注规则**：

| 情形 | 标签 | 说明 |
|---|---|---|
| 水印 | `watermark` | danbooru 现行标签；可加 `blue watermark` / `red watermark` 颜色词 |
| 画师签名/作者标识 | `signature` + `artist name` | 可见才写 |
| 刊名 logo / 商业封面 | `logo` | 无 logo 的图不写 |
| 空对话框 | `speech bubble` | 无文字版的特征 |
| 手写拟态词/音字 | `sound effects` | 属画面元素 |
| 文字族 `text`/`japanese text`/`english text`/`chinese text` | **不写** | danbooru 已废弃整族 |
| 水印本体上的文字 | 不单独标 | 已由 `watermark` 覆盖 |

**为什么「每张都有」还要标**（对齐 2.4 的取舍逻辑）：`uncensored` 删，因为它是**无视觉对应物的 meta 常量**；水印是**有固定视觉对应物的视觉事实**。标了，模型把那片像素归因到 `watermark` 而非画风；生成时省略该标签即可抑制。**不标，水印像素无解释，必然被固化成画风的一部分。**

> ⚠️ 与部分社区文档的分歧：社区主流建议把 `watermark`/`signature` 当 source noise 删除——那是「图能去掉」前提下的最优解。**本指南的前提是「图去不掉」，此时标注优于沉默。** 两者按上面的决策树统一。

> ⚠️ **不要写成恒定断言**：曾出现计划按 N/N 必写 `watermark`，实测只有大部分图有水印（其余已被去字工具去净）。校验断言应为「**与视觉台账逐张一致**」。

## 8.2 角色 tag 阈值

| 规则 | 说明 |
|---|---|
| **只保留出现 ≥4 次的角色** | 实测：250 张摊到 130 个角色，平均不到 2 次；阈值从 10 → 5 → **4** |
| 理由 | 低频角色学不会，只会把画风打散成噪声 |
| 位置 | 紧跟人数标签之后：`1girl, 1boy, katou megumi, …` |
| 拼写来源 | 优先 **animadex**（`https://animadex.net/api/characters/search?q=<空格形名字>&sort=count`，取 `results[].trigger` 的**第一段**，版权段不写）；查不到回退本地 danbooru 词典 |
| 易错点 | `gotou_hitori` → animadex 用 **`gotoh hitori`**（搜 `gotou hitori` 返回 0 条）；`fern` 要带限定词 `fern \(sousou no frieren\)`，而 `frieren` 是裸的 |
| 角色 LoRA | 与上面相反：**不写角色名**（触发词承载），客串不擅自定名 |

## 8.3 人数与关系（必须看图，没有可靠文本兜底）

**背景**：实测某 250 张数据集里 **131 张（52%）是多角色图**，而原始标签**一个大人数标签都没有**。一律推 `1girl` 会把一半以上的图判错。

| 要点 | 说明 |
|---|---|
| 来源 | **视觉回报为权威**；视觉不可用时启发式只作人工参考，**不自动兜底** |
| 数量写法 | 每个数量是**独立 token**：`2girls` + `1boy`、`3girls` + `1boy` |
| 关系标签同归槽位 1 | `multiple girls` `multiple boys` `solo focus` `hetero` `yuri` `bisexual` `group sex` `threesome` `ffm threesome` `orgy` `faceless male` `male focus` |
| `solo` vs `solo focus` | **互斥**。画面确实只有一人写 `solo`；多人但镜头聚焦一人写 `solo focus` |
| 对方未入镜 | 只见手臂/阴茎 → 补 `faceless male` |
| ⚠️ `sex` 无 `penis` 仍算男性在场 | 实测有图有 `sex` 无 `penis`，看图确认画面是 1girl + 1boy（男性只露衣袖、未入镜）。**单看标签会误判成 solo** |
| 废弃写法 | `group` alias 到 `everyone`、`group shot` alias 到 `group picture`——本语境要的是 **`group sex`** |
| **分镜 ≠ 人数** | 同一角色的多视图/三面图，人数按**角色个数**算，多视图写 **`multiple views`**，不要把分镜数当人数写成 `3girls` |

## 8.4 NSFW / 成人内容

- **客观、就事论事、临床式**；不做道德评判、不加修饰、不回避、不淡化
- **性描写标签进槽位 3**，词汇以已验证的成人向成品惯例为准：
  `penis` `pussy` `cum` `cum in pussy` `cum on breasts` `cumdrip` `after sex` `spread legs` `on back` `mating press` `doggystyle` `paizuri` `handjob` `fellatio` `hetero` `yuri` `solo focus` `multiple girls`
- **booru 只打大类，体位细节只能靠看图**（`spread legs`/`on back`/`mating press`/`after sex`/`cumdrip` 站内都不打）——**漏掉就是训练信号缺失**
- **不做道德性删减**：`nipples`/`censored`/`mosaic` 类是视觉事实，照写
- `censored` / `mosaic censoring` / `bar censor` 保留（可见遮蔽是视觉事实）；但 `uncensored` 因为无视觉对应物而删除
- ⚠️ 看图环节可能附带「幼态/未成年」判断且**存在误报**（实测：被判「低龄化」的图权威标签里无 `loli`/`child`/`shota`）。收到拒答**记台账即可**，不要据此改写标注

## 8.5 近重复图

| 情形 | 处理 |
|---|---|
| 同图不同裁剪/放大/截图/水印版/压缩版 | **删除**。重复 = 隐式加权；风格 LoRA 会把特定构图当风格 |
| 差分对（同一构图的细微变化） | 保留，**caption 一致性约束**：仅差分相关标签按实际，其余一致 |
| 字节不同但视觉近似 | 登记到「近重复对表」，**不派给同一看图者**（防印象跨图复制） |
| 实测 | 曾发现同一数据集内 4 对完全相同图；另一数据集 md5 全量 0 对字节重复，但 3 组疑似对经视觉确认为**不同插画**（差集 20~37 tag）——**md5 相同才是重复，近似需视觉确认** |

---

# 九、校验标准

标签内容层面的合格线（执行方法见 `anima-style-lora-caption` skill）：

**格式闸门（离线）**：

- 图-txt 配对完整（N 对）
- 单行、UTF-8 无 BOM、无尾换行
- 无前导逗号、无尾标点
- 全小写空格形、无重复 token
- 触发词置首且**仅首位出现 `@`**
- **BANNED 令牌零残留**：画师标签、IP 系列名、低频角色名、`uncensored` 类 meta
- **否定式 token 零残留**
- **质量词 0**、文字族 0
- 人数一致性冲突 0（`solo`/`solo focus` 互斥）
- 水印区与视觉台账**逐张一致**

**标签有效性闸门（在线）**：

- danbooru `search[name_comma]` 批量查询（每批 120）
- **词典有洞是常态**（部分描写词不在词典内）→ 人工放行白名单
- 校验用下划线原形；角色名带转义括号的注意别被正则截断

---

# 十、排障诊断

## 10.1 症状 → 第一检查

| 现象 | 第一检查 |
|---|---|
| caption 只有前半截生效 | **多行了**。trainer 只取第一行 |
| 训练报 `shuffle_caption, token_warmup_step or caption_tag_dropout_rate cannot be used` | 关了 shuffle 再训（Anima 双缓存下强制） |
| 触发词没生效 | 触发词覆盖率是否 ~100%、是否置首、`keep_tokens` 是否 =1 |
| 风格和固定角色/构图绑死 | **数据集多样性** / caption 是否漏写变化因素 / 固定角色 tag 去留 |
| 某个特征改不动（发色/服装） | caption 里该特征是否**每张都写**——写了才可控 |
| caption 里出现奇怪的词 | 自动打标幻觉 / 口语形 / 否定式，过规范化表 |
| 「我造的标签没效果」 | **查 posts 数**——为 0 就是噪声（`left side ponytail` 教训） |
| 同义词两套写法并存 | 全量检查形态一致性 |
| 训练集大量图带水印且没标 | 水印会被固化成风格的一部分 → 标 `watermark` 或图像层去掉 |
| 人数全写成 1girl | 看图环节是否真的问了人数；多角色图占比 |

## 10.2 「打标相关」的处方链（按序，一次一步）

```
① 数据质量（模糊/重复/水印是否清理）
→ ② 变化因素是否逐张写出（漏写 = 被当成固有特征）
→ ③ 固有特征取舍是否符合目标类型（人物 vs 画风）
→ ④ 标签有效性与形态一致性（废弃/别名/大小写/下划线）
→ ⑤ 是否过标或欠标（20~45 词）
→ ⑥ 质量词与常量标签是否清理
→ ⑦ 触发词覆盖率与位置
```

## 10.3 风格-内容纠缠专项

**症状**：LoRA 激活后老头长出少女脸、白天场景变霓虹夜景、所有输出带训练集的固定构图/角色模板。

**原因**：数据集里某个内容特征与画师风格**强共现**（90% 白发女孩 → `white hair` 与 artist 绑定）。

**处方（按优先级）**：

1. **补反例数据**：男性/老人/动物/建筑/机械/风景（让模型发现白发不是共同因素）
2. **caption 解耦**：把变化因素全部写清楚
3. **删除重复构图图**
4. 降 steps / LR（见 `Anima_LoRA_调参指南.md`）
5. 以上都做了才考虑 dropout

参数侧的排障见 `Anima_LoRA_调参指南.md` 与 `anima-lora-config` skill 的 `assets/troubleshooting.md`。

---

# 十一、附录

## 11.1 通用 Caption 模板

```
@触发词, [1主体] 1girl, solo, <角色名>,
[2外观着装] <发色> hair, <发型>, <瞳色> eyes, <表情>, <视线>, <身材>, <服装>, <饰品>, <身体特征>,
[3姿势动作] <姿势>, <手部动作>, <性描写>, <生殖器>, <体液>,
[4场景环境] <地点>, <道具>, <背景>,
[5光影色调] <day/night>, <sunlight>, <warm tone/cool tone>,
[6构图镜头] <upper body/cowboy shot/full body>, <from above/below>, <close-up>,
[7水印区] watermark, <颜色词>, signature, logo
```

**成品示例（画师风格）**：

```
@myartist, 1girl, 1boy, hetero, solo focus, faceless male, elf, nipples, no panties,
pointy ears, breasts out, wet, sweat, saliva, blonde hair, long hair, straight hair,
hair intakes, blue eyes, half-closed eyes, tears, small breasts, skinny, pale skin,
wide hips, blush, open mouth, drooling, anus, ass, grabbing another's ass, penis, pussy,
sex, skirt lift, vaginal, girl on top, upright straddle, large insertion, pussy juice,
weapon, soft lighting, backlighting, warm tone, day, close-up, cowboy shot, crotch focus,
lower body, from behind, ass focus
```

**成品示例（角色，简洁型）**：

```
@mychar, 1girl, solo, purple hair, very long hair, double bun, blunt bangs, hair ornament,
tassel earrings, forehead jewel, red eyes, black shirt, white dress, frills, cleavage cutout,
brooch, gem, white thighhighs, blush, looking at viewer, skirt lift, pussy, white background,
simple background, cowboy shot
```

## 11.2 打标检查清单（每张图过一遍）

- [ ] 触发词在，且拼写完全一致
- [ ] 单行、全小写、空格形、`, ` 分隔、无 BOM、无尾换行、无前导逗号
- [ ] 无重复 token
- [ ] 每个标签都**在画面里真实存在**（无幻觉）
- [ ] **没有否定式**（`no cum` 类）
- [ ] 无质量词（除非逐张测量过）
- [ ] 无画师标签 / IP 系列名 / meta 常量
- [ ] **变化因素全部写出**（姿势/服装/表情/背景/镜头/光照）
- [ ] 固有特征取舍符合目标类型
- [ ] 人数与关系正确（看图得出，`solo`/`solo focus` 不并存）
- [ ] 所有标签都是**真实存在的 danbooru 标签**（或白名单描写词）
- [ ] 与数据集其他图的**形态一致**（同一事实只有一种写法）

## 11.3 常用阈值速查

| 项 | 值 |
|---|---|
| 标签数区间 | 20~45（均值 30~39） |
| 角色 tag 阈值 | ≥4 次 |
| 固有特征剪切阈值 | 出现比例 ≥0.9 才可剪 |
| WD14 阈值 | eva02 ≈0.53 / swinv2 ≈0.27；角色 0.85 |
| 在线校验批量 | 120/批 |

## 11.4 事实来源

**官方与社区（联网检索）**
- [circlestone-labs/Anima 模型卡](https://huggingface.co/circlestone-labs/Anima)：标签段顺序、`@` 画师前缀、全小写空格、质量词、混合用法
- [Anima 官方讨论区 #205 Training Tagging Specifications](https://huggingface.co/circlestone-labs/Anima/discussions/205)、[#96 Strategy for captioning](https://huggingface.co/circlestone-labs/Anima/discussions/96)：社区实操经验
- [lora-training-skill caption-guide](https://github.com/Rinne414/lora-training-skill/blob/main/references/caption-guide.md)：格式核实（含 trainer 源码行号）、caption 悖论、WD14 阈值、trait prune ≥0.9、蕴含父标签折叠、kaomoji 陷阱
- [AnimaLoraStudio 中文文档](https://github.com/WalkingMeatAxolotl/AnimaLoraStudio)：端到端流水线、三种打标器
- [WAI-Anima caption rework 记录](https://lilting.ch/en/articles/wai-anima-lora-caption-rework)：`left side ponytail` 不存在标签的教训、方向信息交给自然语言
- [Civitai: A Complete Beginner's Guide to Local Anima LoRA Training](https://civitai.com/articles/31678/a-complete-beginners-guide-to-local-anima-lora-training)：`@` 触发词、keep_tokens=1、过标/欠标判断
- [Civitai: The Ultimate LoRA Tagging Guide](https://civitai.com/articles/29347/the-ultimate-lora-tagging-guide-from-dirty-image-to-perfect-model)：只标看得见的、不打否定标签、同义词统一
- [danbooru-tag-expander](https://github.com/allenday/danbooru-tag-expander)：别名是**有向**的（antecedent → consequent），据此判定废弃与现行形

**Trainer 源码（已核实）**
- Anima-Standalone-Trainer `library/train_util.py`：`:872` 只取第一行；`:4515-4538` shuffle/keep_tokens 定义；`:891` 按 `caption_separator` 切分
- `anima_train_network.py`：`:63` TE 缓存与 shuffle/dropout 互斥断言；`:48-59` subset caption dropout 上提为全局
- `library/anima_train_utils.py`：`:101,107` `qwen3_max_token_length` / `t5_max_token_length`
