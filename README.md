# 洪滨每日AI早报

每天北京时间 05:30 由 GitHub Actions 尝试生成 4 条主新闻和 3 条科技人物动态，供编辑审核后发布。截图使用仓库原有的 `index.html` 和 `globe.png`，保留原期号规则。

## 本地测试

```sh
python -m pip install playwright pillow
python -m playwright install chromium
python -m unittest -v
python daily_auto.py --test
python daily_auto.py --test --as-of 2026-11-21T05:30:00+08:00
```

测试模式只写 `test-output/`。每次写入 `source_report.json`，质量不足时同时生成 `editorial_review.json`。报告逐来源记录实际采集方式、本次访问结果、最近成功时间、可校验日期的条目数、主题过滤量及入选量。指定截止时间只回放当前仍可取得的来源。生成器可通过 `HONG_BIN_CHROMIUM` 指定已安装的 Chromium 可执行文件。Windows 上可以使用微软雅黑，Linux 应安装 Noto CJK。

## 采集与审校

- 来源登记覆盖机构官网、专业媒体、综合媒体、聚合渠道和人物跟踪。RSS/Atom 按订阅格式读取，网页只采公开且可追溯的原文。Google News、Hacker News、GitHub Trending 和 AIHOT 用于发现。AIHOT 解析 `links.original`，热度和来源名称均不参与排名。
- 主新闻严格使用 18 小时窗口。科技人物使用 48 小时窗口，须有明确身份、具体事件、可追溯的来源及独立摘要，图上显示北京时间，JSON 同时保留完整时间。人物关注名单可在 `news_sources.json` 更新。
- 使用规范化原文 URL、标题和型号及事件组合与历史做去重。对商业宣传、博彩、弱摘要、未经确认的重大消息和模板溢出采取拦截。
- 通过筛选的条目仍需编辑核对上下文和图像版权。不可验证日期、翻译失败、站点限流及来源不足会减少入选量。字面去重无法覆盖所有跨语种改写。
- `editorial_media.json` 保存有出处的同一事件备用图，以及经核实的人物职务和肖像。若下载或画布解码后的图片不足 7 张，对应候选进入待处理报告，禁止导出含占位框的 PNG。

## Actions

推送触发测试。主分支定时运行正式模式，也可在主分支手动运行并关闭「仅生成测试产物」。本地正式模式使用 `python daily_auto.py --formal`。

质量门槛通过后才更新正式 JSON、PNG 和期号历史，Actions 将三者作为单个 Git 提交写回主分支。执行期间若主分支有新提交，推送会失败并保留远端现状。栏目不足或出图失败时上传待审报告，正式成品和历史不受影响。
