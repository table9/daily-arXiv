# daily-arXiv 调度检查与日期补采

调度状态复核日期：2026-10-24（Europe/Berlin；UTC 记录时间为 2026-10-23 23:03:02）。基线仍为 `main@70b745dfa3eb3f0beffd89953c86c6e4a16c542f`。
本变更提供可审查的恢复能力。2026-10-22 已有草稿 [PR #1](https://github.com/table9/daily-arXiv/pull/1)，本次继续审查该分支、修正失败保护并刷新调度证据。696 条候选 ID 预检保留为 10 月 22 日记录，未在本次重新验证。尚未执行元数据补采、AI 调用或日报发布。

## 已证实与未证实

| 检查项 | 实测结果 | 结论边界 |
|---|---|---|
| 工作流 REST 状态 | `181557912` 为 `active` | 当前没有被工作流级禁用；不能据此重建过去的配置变更 |
| 默认分支与文件 | `main`；存在 `arxiv.yml`；cron 为 `00 19 * * *`，支持手动触发 | 最后一次成功运行对应的工作流 blob 与当前基线一致 |
| 最近 arXiv 运行 | `38482634570`，`schedule`，2026-10-13 23:10:52 UTC，`success` | 爬取、去重、AI、Markdown、提交和推送均成功 |
| 最近所有 Actions 运行 | Pages 运行 `38483961848`，10-13 23:23:38 UTC，成功 | 不应把 Pages 运行误当作 arXiv 定时任务 |
| 10-14 至检查日的运行记录 | 日期过滤 API 返回 `total_count: 0` | 没有可见的失败、排队或取消运行可解释缺口 |
| 仓库状态与分支更新 | 未归档、未禁用；仓库 `pushed_at` 已为 10-22 19:43:18 UTC；main 仍为 `70b745d` | 10 月 22 日 PR 分支推送不等于 main 日报恢复；main 最近日报提交仍为 10 月 13 日 |
| 10-14 至 21 日原始、增强和 Markdown 文件 | 均不存在 | 尚未恢复日报 |

具体响应字段和来源见 [本次调度复核快照](actions-audit-2026-10-24.json)；[10 月 22 日检查快照](actions-audit-2026-10-22.json) 原样保留。新快照的日期过滤为 `2026-10-14..2026-10-24`，截至读取时仍返回零记录。**根因尚未确认**。证据首先指向运行创建/触发这一层，不能把缺口归因于运行后的爬虫失败或去重跳过；也不能排除曾删除运行等不可见历史。

尚须使用具有相应权限的 GitHub 登录核实仓库 Actions 总开关/允许的 Actions、账号或组织级限制，以及调度关联 actor 的状态。公开 GET `/actions/permissions` 返回 401，不能据此判断开关值。本次没有读取任何 secret 的内容，也未修改线上开关。

GitHub 官方说明：整点高负载可能延迟或丢弃定时事件；工作流 `active` 时，关联 actor 异常也可能导致 cron 停止；写权限用户修改 cron 可重新绑定调度 actor。本变更把触发时间移至 **19:17 UTC**，但不把这项缓解措施视为已经找到了根因或保证调度恢复。60 天无活动停用是平台规则，本次状态没有显示 `disabled_inactivity`。

- [GitHub 调度故障排查](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
- [GitHub schedule 事件与 actor](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 代码变化

| 文件 | 作用 |
|---|---|
| `.github/workflows/arxiv.yml` | 增加单日/日期区间、计划检查和发布选项；串行发布；显式写权限；PR 仅运行离线测试；保留原 HTTP 诊断 artifact |
| `scripts/run_dates.py` | 统一目标日期，按时间顺序调原流程，在隔离目录验证后安装结果，保护已有文件并生成检查凭据和运行报告 |
| `daily_arxiv/daily_arxiv/spiders/arxiv.py` | 有日期时访问 `/catchup/分类/YYYY-MM-DD`；检查页内日期、计数和分页完整性；统计丢页/错误/预期条目。无日期的旧爬虫入口仍访问 `/new` |
| `daily_arxiv/daily_arxiv/check_stats.py` | `--date`、`--data-dir`；从目标日期回溯七天；保留原历史去重规则，同时去掉日内重复；损坏历史文件按错误处理 |
| `tests/test_backfill.py` | 验证日期、历史去重、分页、无公告、断点续做、重复运行及失败保护；调用真实 Markdown CLI，mock 网络与 AI |
| `tests/test_publication_atomicity.py` | 注入发布失败、检查点失败和并发冲突，验证单日新增文件回滚、原文件与重跑 SHA-256 保持不变 |
| `tests/test_catchup_integrity.py` | 验证同一公告跨页总数一致性，拒绝页面总数变化造成的静默漏采 |

AI 仍使用 `ai/enhance.py`、既有模型/语言/secret；Markdown 仍由 `to_md/convert.py` 生成。没有修改提示词或更换模型。

### 日期口径与边界

`--date` 表示 **arXiv catchup 页面标示的公告日期**。它不是作者首次投稿的 UTC 日期，也不是重跑当天。日常调度无输入时选择 UTC 当天的公告日期；原有文件保持原来的归档标签，既有结果不会被重新归日。

arXiv API 的 `submittedDate` 不能精确重建每日公告队列，所以本实现没有用投稿日期查询去近似公告。来源为 arXiv 自己的 [catchup 实现](https://github.com/arXiv/arxiv-browse/blob/develop/browse/controllers/catchup_page.py)，限最近约 90 天；超出范围、未来日期、反向区间、单日与区间混用均在写入前报错。

采集保持现有的主分类匹配规则，以及 new/cross 条目选择，不加入 replacement。分类列表里的 `cond-mat` 沿用原有归档入口行为，不借此扩大到原主分类过滤器未接受的类别。元数据由原 pipeline 获取，可能是当前版本的元数据；不能声称恢复了历史时刻的摘要/分类快照。

### 重跑和失败处理

- 已有有效原始 JSONL、AI JSONL 和非空 Markdown：跳过；不重调 AI，不覆盖内容。
- 只有原始 JSONL：从该文件继续，保留其字节，不重新爬取或重新扣除历史 ID。
- 原始与 AI 数据都在、Markdown 缺失：复用 AI，只生成 Markdown。
- 已有文件损坏、AI 为 `Error`/缺项、ID 不一致、凭据哈希变化或配置不一致：停止并保留文件，要求具体修复后再重试；不提供强制覆盖开关。
- 新日期先在 `.arxiv-work/` 或指定 `--work-dir` 内完成采集、七日去重、AI、Markdown；检查通过后使用原子安装，拒绝覆盖不同内容。
- 10 月 24 日审查修正了原草稿逐文件安装时可能留下失败日残片的问题：整日预检后发布，任一文件或检查点安装失败即回滚本次新增文件。已有文件保留。工作流在进程中断、尚无完整运行报告时不提交本地输出，产物留在 artifact 供审查。
- 同一分类公告的分页总数必须与第一页一致；不能把后续页缩小总数导致的少采当成完整结果。复用旧 raw 的凭据标为 `existing_raw_archive_date`，不将旧归档日期改称新取得的 catchup 来源。
- Scrapy 即使退出 0，只要有错误统计、缺页、截断或采集条数不符，也按失败处理；AI 脚本返回 0 但单篇增强不完整同样失败。
- 完整检查后没有新内容：只提交 `assets/harvest-status/YYYY-MM-DD.json`；不伪造有论文的日报。它和失败/未执行日期可明确区分。
- 区间在首个失败日期停止；此前已验证日期可独立提交，后续标为 `not_attempted`。Actions 作业仍为失败，不能把部分成功称为全段成功。重跑同一区间会跳过完成日期。
- 报告、暂存输出和原 HTTP 诊断会上传到 Actions artifact，保留 30 天；手动 `publish=false` 可先审查产物。计划检查 `dry_run=true` 完全不访问网络、不调用 AI，也不写日报。

## 10 月 22 日保存的 10 月 14–21 日只读预检

以下是 **10 月 22 日旧预检**：使用最后一次成功运行日志中的分类配置，读取 `cond-mat` 归档与 `physics.chem-ph`、`physics.comp-ph` 的 catchup 页，并应用原主分类过滤器。该预检没有执行元数据查询或 AI。本次未重新验证完整候选集合，不将旧数量升级为当前已核实数据。

| 公告日期 | 日内唯一候选 ID | 按现有及拟补七日历史模拟去重后 |
|---|---:|---:|
| 2026-10-14 | 230 | 230 |
| 2026-10-15 | 81 | 81 |
| 2026-10-16 | 91 | 91 |
| 2026-10-17 | 0 | 0 |
| 2026-10-18 | 0 | 0 |
| 2026-10-19 | 85 | 85 |
| 2026-10-20 | 128 | 128 |
| 2026-10-21 | 81 | 81 |
| 合计 | 696 | 696 |

详细来源见原样保留的 [10 月 22 日候选预检](backfill-preview-2026-10-14_to_21.json)。696 是旧只读调查的候选数，须在本次实际采集时重新验证；不是已经生成的增强摘要。最终数量仍须以真实补采的完整元数据、分类和去重检查为准。旧预检中 17、18 日的零候选也需通过本次完整采集确认，不改变这两天没有可见 Actions 运行记录这一事实。

## 验证与恢复操作

### 本地离线验证（10 月 24 日已执行）

```bash
uv sync --frozen
.venv/bin/python -m unittest discover -s tests -v
actionlint .github/workflows/arxiv.yml
```

本次使用 Python 3.12 和原 `uv.lock` 锁定依赖（Scrapy 2.12.0、arxiv 2.1.3），完整执行 **35 项测试，全部通过，38.653 秒**。测试包含真实 Scrapy 下载器、分页回调、pipeline 失败信号、统计与 feed 导出，以及真实 Markdown CLI、硬链接发布与回滚；网络页面和 AI 用本地替身，未调用真实 arXiv 元数据或 AI 服务。

`actionlint 1.7.12`、全部 6 个 shell 步骤的 `bash -n`、`git diff --check` 均通过。八日 `--dry-run` 计划已重新执行，均为 `would_crawl`；检查后 `data/`、`md/`、`assets/file-list.txt` 无差异。额外读取旧 10 月 13 日数据，确认其 170 条 raw 含 79 个唯一 ID，与 79 条 AI 结果一致，现有结果被识别为 `skip_complete`，没有将旧 raw 的重复记录视为需要重写历史文件的理由。

使用最后成功运行中的分类配置规划区间；以下命令已于本次重新执行且未写日报。该配置来自旧运行日志，恢复前仍须检查当前仓库变量：

```bash
export CATEGORIES='cond-mat,cond-mat.str-el,cond-mat.mes-hall,cond-mat.mtrl-sci,cond-mat.supr-con,cond-mat.stat-mech,physics.chem-ph,physics.comp-ph'
.venv/bin/python scripts/run_dates.py --start-date 2026-10-14 --end-date 2026-10-21 --dry-run
```

本地实际补采时去掉 `--dry-run`，并通过既有环境配置提供 API secret、`MODEL_NAME`、`OPENAI_BASE_URL` 和 `LANGUAGE`。不要使用仍按当天删除重建的旧 `run.sh` 来补历史日期。

### GitHub 配置复核（尚未执行以下认证操作）

```bash
gh api repos/table9/daily-arXiv/actions/workflows/arxiv.yml --jq '{id,state,path}'
gh api repos/table9/daily-arXiv/actions/permissions
gh variable list -R table9/daily-arXiv
```

确认当前分类/语言/模型与预期一致，以及作者 `NAME`、`EMAIL` 和原 AI secret 的名字仍可用；只验证 secret 存在，不输出其值。仅在 `state` 实际为 disabled 时才执行 `gh workflow enable arxiv.yml -R table9/daily-arXiv`；当前已读到 `active`，无需盲目关闭再开启。手动运行成功仅证明手动入口可用，之后仍须观察 `event=schedule` 的新记录。

### 合并后规划、预览产物、正式补采（本次均未执行）

先规划，不消耗 AI：

```bash
gh workflow run arxiv.yml -R table9/daily-arXiv --ref main -f start_date=2026-10-14 -f end_date=2026-10-21 -f dry_run=true -f publish=false
```

需要先审查完整产物时，将 `dry_run` 设为 `false`、保持 `publish=false`，下载 `arxiv-recovery-*` artifact 审查。此预览会调用 AI，但不推送日报；再次单独启动正式运行会重新调用 AI，因为预览产物不会自动写回 main。

审查代码后的一次正式恢复命令（会调用 AI，并把完整日期的结果提交到 main）：

```bash
gh workflow run arxiv.yml -R table9/daily-arXiv --ref main -f start_date=2026-10-14 -f end_date=2026-10-21 -f dry_run=false -f publish=true
```

单日示例：用 `-f date=2026-10-14` 替代两个区间参数。不要同时提供 `date` 与区间。

观察并验收：

```bash
gh run list -R table9/daily-arXiv --workflow arxiv.yml --limit 10
gh run watch RUN_ID -R table9/daily-arXiv --exit-status
gh run download RUN_ID -R table9/daily-arXiv
```

1. 运行报告应覆盖 8 天；逐日以实际检查结果标为 `complete` 或 `no_new_content`，不得预先照抄旧预检的 6 个非空日/2 个空日，也不得把 `failed` 或 `not_attempted` 当成恢复完成。
2. 检查原始/增强 ID 集合一致、AI 五个字段有效、Markdown 日期正确、file-list 和对应凭据齐全。脚本已在安装前执行这些数据检查，凭据保存结果 SHA-256 供复核。
3. 再运行同一区间：应为 `skip_complete`/`skip_no_new_content`，文件内容与哈希不变，不重调 AI。
4. 在下一次 19:17 UTC 调度之后核实出现 `event=schedule` 的运行；允许平台延迟。若手动成功但仍无定时记录，应携带本检查快照、运行链接和权限状态向 GitHub 排查调度，不宣称 cron 修改已经解决根因。

若补采失败，只重试同一区间即可跳过已发布的完成日期。若是 AI 缺项或网络错误，未验证结果只留在暂存目录/artifact；既有日报被保护。若是已有文件与凭据不一致，应先审查该具体文件再恢复，不能直接删除历史目录。
