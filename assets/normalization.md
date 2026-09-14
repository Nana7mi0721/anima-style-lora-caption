# 标签规范化与校验

配套 SKILL.md §5、`assets/workflow.md` §5~§7。本文件是**可执行的对照表与操作方法**；背后的原则见 `references/Anima_style_LoRA_打标指南.md`。

---

## 1. 归一化表（VISION_FIX）

视觉层与权威标签转换得到的 token **都要**过这张表。合并阶段自动生效，**不要靠提示词去约束子代理**（子代理只管如实描述，规范形由合并层统一处理）。

### 1.1 别名 → 现行形

| 原词 | 现行形 | 说明 |
|---|---|---|
| `pantsu` | `panties` | 别名 |
| `seifuku` | `serafuku` | 别名 |
| `megane` | `glasses` | 别名 |
| `naked` | `nude` | 别名 |
| `topless` | `breasts out` | **已废弃无别名**，换具名等价标签（信息不可丢） |
| `ass_grab` | `grabbing another's ass` | **已废弃无别名** |
| `breast_grab` | `grabbing another's breast` | 别名 |
| `breast_hold` | `grabbing own breast` | **已废弃无别名** |
| `tan_lines` | `tanlines` | 别名 |
| `bunny_ears` | `rabbit ears` | 别名 |
| `swimsuits` | `swimsuit` | 别名 |
| `bikini_top` | `bikini top only` | 别名 |
| `semen` | `cum` | 已废弃 |
| `bikini_pull` | `clothes pull` + `one breast out` | 已废弃，拆成两个现行标签 |
| `nopan` | `no panties` | — |
| `shimapan` | `striped panties` | — |
| `smiling` | `smile` | 非 danbooru 词 |
| `slender` | `skinny` | 视觉层实测 |
| `group shot` | `group picture` | 视觉层实测 |
| `on all fours` | `all fours` | 视觉层实测 |
| `hair clip` | `hairclip` | 视觉层实测 |
| `hairy arms` | `arm hair` | 视觉层实测 |
| `reverse cowgirl` | `reverse cowgirl position` | 视觉层实测 |
| `tights` | `pantyhose` | 词典有 pantyhose |
| `sofa` | `couch` | 词典有 couch 无 sofa |
| `indoor background` | `indoors` | — |
| `outdoor background` | `outdoors` | — |
| `low angle` | `from below` | — |
| `high angle` | `from above` | — |
| `thigh-highs` | `thighhighs` | — |
| `hand on face` / `hand on cheek` | `hand on own face` / `hand on own cheek` | — |
| `front view` / `back view` / `side view` | `straight-on` / `from behind` / `from side` | 前者 posts=0，**不存在** |
| `looking away` | `looking to the side` | 已废弃 |

### 1.2 废弃 → 删除（信息由别的标签承载）

| 原词 | 处理 |
|---|---|
| `areola` / `areolae` | **删除**（两者均废弃且无别名），乳晕信息由 `nipples` 承载 |
| `bangs` | **删除**（已废弃） |
| `text` / `japanese text` / `english text` / `chinese text` | **整族不写**（已废弃），改由 `speech bubble` / `sound effects` / `logo` 承载 |

### 1.3 语义陷阱（必须显式覆盖）

| 原词 | 陷阱 | 处理 |
|---|---|---|
| `cowgirl` | 有 active alias 指向 `cow girl`（牛仔女郎**服装**） | 若语境是**体位**，必须写 `cowgirl position`，不能直接吃 alias |

> 一般规则：**吃 alias 前先确认语义**。别名表只保证词形合法，不保证语义正确。

### 1.4 颜色与复合形

- **词典无的复合色降级为泛色**：`light blue hair`→`blue hair`、`silver hair`→`white hair`、`golden/amber eyes`→`yellow eyes`
- **同图 ≥2 种发色** → 加 `multicolored hair`
- **并存同类**（`dress` + `white dress`、`bow` + `blue bow`）→ **保留具体形，删泛形**

### 1.5 折叠蕴含父标签

有具体标签时删掉泛化父标签：有 `cat ears` 就删 `animal_ears`；有 `school swimsuit` 就删 `swimsuit`。

> ⚠️ 例外：若父标签是**故意留白**的（例如让视觉层的具体种属字段独占，而权威标签的标注不可靠），要写进动作表 `note` 说明，否则复查时会当成漏删。

### 1.6 否定式（两层防护，缺一不可）

| 层 | 做法 |
|---|---|
| 提示词层 | 写明「看不到就写 `none`，不要写否定式」 |
| 合并层兜底 | `^(no\|without\|not)\b` 一律丢弃 |

⚠️ **例外必须保留**：`no panties` / `no bra` / `no panties` 这类**本身就是标签**的词，不能被否定式规则误杀。

### 1.7 通用形改写兜底

子代理会产出大量口语形或空标签，合并层需有通用改写：

| 模式 | 处理 |
|---|---|
| `N girls X` | 规范为数字形 |
| `X visible` / `X focus` | 按现行标签改写或丢弃 |
| `slightly X` | 去掉程度副词 |
| `close-up on …` | 改为 `close-up` |
| `X background` | 改为 `indoors` / `outdoors` 或具体标签 |
| `<角色名前缀> <特征>`（如 `kanne brown eyes`） | **剥离角色名前缀** → `brown eyes` |

> ⚠️ 角色名前缀剥离会误伤白名单描写词（实测 `bright indoor light` 被剥成 `indoors`）。**白名单词必须在剥离规则之前提前放行。**

---

## 2. 描写词白名单

danbooru 没有对应的光影/构图标签，但**对画风 LoRA 是可学的视觉信号**，需登记放行，否则会被判为「未知标签」或误伤。

**收录标准**：语义明确、可复现、不与已有词高度重复。**纯散文句一律丢弃**（如 `looking at the scene`、`two girls arranged horizontally in frame`）。

**光影类**：

```
warm tone          cool tone           soft lighting
soft light         bright lighting     natural lighting
warm lighting      dim lighting        cinematic lighting
soft shadows       side lighting       strong light
strong shadows     hard shadow pattern low contrast
cool-warm mixed    bright indoor light sunlight from left
sunlight through blinds                 soft body highlight
```

**构图类**：

```
diagonal composition   multi-figure composition
eye level              foreground focus on hips
```

**水印颜色词**：`blue watermark` / `red watermark`

⚠️ **白名单是解析与合并两层共用的**。改白名单后**必须先重跑解析再合并**，否则词在解析阶段就已被丢弃。

---

## 3. 词典与在线校验

### 3.1 词典准备

| 项 | 要求 |
|---|---|
| 来源 | 从 danbooru API 拉取 tag 表（含 `name` / `category` / `post_count` / `is_deprecated`） |
| 🔴 排序参数 | **必须按使用量降序**。默认按 id 倒序（最新标签在前），会一路抓刚创建的长尾标签，**抓几十万行也抓不到 `nude` / `1girl` 这类高频标签**（已实测踩过） |
| 规模参考 | 按使用量降序抓取，几十万行即可覆盖全部常用标签 |
| 废弃标记 | `is_deprecated=1` 必须拦下，改用现行形 |
| 用途 | **只当离线缓存**；真正的闸门是在线批量查询 |

### 3.2 校验形态

| 校验 | 用哪种形态 | 原因 |
|---|---|---|
| 词典比对 | **下划线原形** | 词典是下划线格式，实测空格形查不到 |
| 写回产物 | 空格形（`score_*` 除外） | 与底模训练分布一致 |

⇒ **先用原形校验，通过后再做空格化 / 目标模型转换。** 转换后的特殊标签（如带限定词的角色名、触发词）加进 `KNOWN_EXTRA` 白名单。

### 3.3 在线批量查询

- 接口：danbooru `search[name_comma]`
- 批量：**每批 120 个**
- 🔴 批量查 `tags.json` 时**必须带 `limit=len(批次)*2`**（`limit=0` 只回 1 条）
- 分来源分层校验：词典只覆盖 booru 侧转换来的标签；**视觉层补的描写性标签**本来就不在词典内，只做宽松核对，不要一刀切判未知

### 3.4 词典有洞是常态

实测常见缺失：`teary eyes` / `shower` / `soft lighting` 等——它们可能是有效标签但不在抓取窗口内。处理方式：

1. 先在线的 `search[name_comma]` 复核
2. 仍查不到但确实在成品集里大量使用的 → 人工放行白名单
3. 查不到且无先例 → 按未知处理（不要放行自造词）

---

## 4. 专项断言（写进校验脚本）

```
□ 触发词置首，且 @ 只出现在首位触发词
□ 单行、全小写、无重复 token、无前导逗号、无尾标点
□ BANNED 令牌零残留：画师标签 / IP 系列名 / 低频角色名 / meta 常量
□ 否定式 token 零残留
□ 质量词 0
□ 文字族 0（text / japanese text / english text / chinese text）
□ 人数一致性冲突 0（solo 与 solo focus 互斥）
□ 水印区与视觉台账逐张一致
□ 图-txt 配对 N 对完整
□ 近重复对逐对一致性
```

> 「水印区与台账一致」而非「每张必含 watermark」：水印是否恒定必须由视觉实测决定，**不要写成恒定断言**——实测出现过「计划按 N/N 必写，实际只有大部分图有水印」的情况。
