# 线上(8.221.106.221) 与本地代码/数据库差异报告

生成时间：2026-09-04
说明：本报告基于 **filesystem checksum（rsync -c）** 与 **information_schema / alembic_version** 实测，
用于上线“算法推荐”前明确需同步的代码文件、以及数据库层面是否需要补字段。

---

## 一、代码差异清单（线上 /opt/play_video/ivapp/app vs 本地 app/）

### A. 内容不同的文件（checksum 确认，本地比线上新）
| 文件 | 线上(改动前) | 本地 | 关联提交/说明 |
|------|------|------|------|
| `app/config.py` | 207 行 | 234 行 | ab909d9 算法推荐 / a38f030 本次优化 |
| `app/impressions.py` | 113 行 | 266 行 | ab909d9：Redis 推荐池、RedisRecommendStore、user:seen |
| `app/routers/feed.py` | 1116 行 | 1564 行 | bfbb00c + ab909d9 + a38f030（本地的抽样/排序优化） |
| `app/schemas.py` | 905 行 | 933 行 | 新增 `is_rewind` 等 Feed 字段 |
| `app/main.py` | 旧 | 新 | bfbb00c：挂载 /seen、/test 等路由 |
| `app/protocol_envelope.py` | 旧 | 新 | bfbb00c：新增 `seen_error` 等响应辅助 |

> 上面 main.py / protocol_envelope.py 之所以也是差异，是因为线上不仅落后“算法推荐”一个提交，
> 还落后于 `bfbb00c`（新增 /seen 上报表、feed level 档位字段、/test 测试页）。这些是与
> 算法推荐配套的前置提交，需一并上线，否则会出现“缺 seen_error / 函数导入失败”。

### B. 本地有、线上缺失的文件（`++++`）
| 文件 | 说明 |
|------|------|
| `app/recommend_pool_builder.py` | 内容池重建（影子+RENAME），算法推荐核心模块 |
| `app/test_home.html` | bfbb00c 的 /test 测试页（非运行时必需，可上线） |

### C. 无需处理
- 大量 app 内 `.py` 仅 mtime 不同、内容一致（rsync `t`，无 `c`），不计入同步。
- 线上 app 下不存在“本地已删”的 py（无 `deleting`）。

### D. 结论（要完整上线这套功能，至少需同步）
`app/config.py`、`app/impressions.py`、`app/routers/feed.py`、`app/schemas.py`、
`app/main.py`、`app/protocol_envelope.py`、`app/recommend_pool_builder.py`、`app/test_home.html`
（外加 `scripts/compose_target.sh` 等部署脚本是否一致需另行核查；本报告聚焦 app/ 源码）。

> 注意：因文件间存在 import 依赖（feed 依赖 impressions 的 content_pool_key / schemas 的 is_rewind /
> protocol_envelope 的 seen_error），**不能只挑 feed.py/config.py 上**，必须配套整组，否则 API 启动失败（已实测验证）。

---

## 二、数据库差异结论（无缺字段，需补的是“数据档位”）

### 1. 迁移版本
- 本地 alembic head：`20260901_0018`（`migrations/versions/20260901_0018_published_video_seo.py`）
- 线上 DB `alembic_version.version_num`：`20260901_0018`
- **两者一致，无需跑迁移。**

### 2. 表结构对比（information_schema 实测）
对 models 中所有业务表逐列比对：**线上 DB 不缺任何 models 里定义的字段或表**。
本地 metadata 有而线上无：无。
线上多出（均为运维/临时，不影响）：`alembic_version`、`feed_author_repair_20260816_feed_home`。

### 3. “published_videos 没配置档位”的真实含义
- 档位**不需要新增数据库列**，它使用已存在的 `feed_weight` 字段（线上 `published_videos` 30 列中已含 `feed_weight`）。
- “没配置档位” = `feed_weight` 数据仍为默认 0、尚未被标注成 1~5（运营侧没打标），或是内容池尚未按它重建。
- 需要补的是**数据**（把视频按质量标成 1~5 并触发 `recommend_pool_builder` 重建 5 档 Redis 内容池），
  不是 DDL。

---

## 三、上线动作建议（若决定全量同步）
1. 用官方 `scripts/deploy.sh --environment production`（会整仓 rsync + 校验 + DB 迁移 + 健康检查 + 自动回滚）。
   - 前提：本地 worktree commit（或 `--allow-dirty`）、准备 `.deploy.production.env`。
   - 该流程**不会**开启 Redis 推荐开关（线上 env 无 FEATURE_RECOMMEND_REDIS）、**不会**改动 published_videos 数据/档位。
2. 若坚持“只传源码”，请务必按第一部分的 8 个文件**整组**同步（不能挑文件），并 min 只重启 api；
   但仍建议先跑 `deploy.sh --dry-run` 做校验。

## 四、遗留待澄清
- 线上部署基线对应哪个 git commit 尚不明确（服务器 rsync 排除 .git，无版本锚点），
  本报告以 filesystem 实际差异为准，是最可信口径。
