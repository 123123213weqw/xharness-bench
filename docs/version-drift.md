# 版本漂移：对比跑的是哪个上游

**结论先说：这个 bench 现在回答的是"复刻得像不像"，不是"跟今天的官方比谁强"。**

## 三个不同的"上游"

| 名称 | 版本 | 日期 | 用在哪 |
| --- | --- | --- | --- |
| 冻结点 | `141eb6fef8` = `dsh-v0.1.0-rc.8` | 2026-08-19 | XHarness 实现的契约 |
| bench 的 dsh 臂 | SDK `0.1.0rc7` = `dsh-v0.1.0-rc.7` | 2026-08-17 | 对比的另一方 |
| 上游最新 | `dsh-v0.1.6-alpha.1` | 2026-09-15 | 没人跑过 |

三个数都不一样，而且 **bench 的 dsh 臂比我们自己的冻结点还早一个 tag**。原因是 rc.8 从来没发布到 PyPI，适配器选了"最接近冻结点的可安装版本"，也就是 rc.7。

所以配对结果的正确读法是：

> XHarness（rc.8 契约） vs 官方（rc.7）

这在**契约层面大概等价**：2026-08-21 的增量审计显示 rc.8 → 0.1.1-rc.2 之间固定 RPC 名称集合无变化。作为"复刻保真度"的检验，这个对比是成立的。

它**不能**回答"用户该用哪个"，因为官方此后又发了 16 个 tag。

## 那 16 个 tag 里发生了什么

上游最后一次被审计是 2026-08-21，只走到 `0.1.1-rc.2`，结论是"暂不移动冻结基线"。再往后没有任何审计记录。而后面这些不是小修：

| 版本 | 变更 | 性质 |
| --- | --- | --- |
| 0.1.2-alpha.1 | 旧版调用接口 `ApiProxy` 迁移并移除，改用 `@Remote` 网关 | 破坏性 |
| 0.1.2-alpha.4 / rc.1 | `Session.events` 被按需读取 API `seq` / `eventAt()` / `snapshotEvents()` 取代 | 破坏性 |
| 0.1.5-alpha.1 | 移除 `ctx.agent`；`Inbox` 改为类型接口，`hasPending` / `claim` 移出公共接口 | 破坏性 |
| 0.1.5-alpha.2 / rc.1 | **会话数据格式升级至 V3**，旧日志迁移后不支持降级读取 | 破坏性 |
| 0.1.5-rc.1 | Session persistence 改为生命周期持有的 `SessionHandle`；`agentLoop.create()` 改异步；新增 session 锁（同一 session 至多一个进程持有） | 破坏性 |
| 0.1.5-rc.1 | Web 插件面板：`sidebar.panellist`，原 `conversation` Slot 迁到 `main` | 破坏性 |
| 0.1.6-alpha.1 | **DeepSeek 默认协议改用 Messages**，官方根地址变 `https://api.deepseek.com/anthropic` | 破坏性 |
| 0.1.6-alpha.1 | 弃用同步历史读取接口 `snapshotEvents` / `eventAt` / `ownEvents` | 破坏性 |
| 0.1.6-alpha.1 | MCP 升级到官方 SDK v2，协议协商、分页工具列表 | 破坏性 |
| 0.1.6-alpha.1 | Headless 支持从 stdin 读任务、`--session-id` 恢复、`--json` 输出 ndjson 事件 | 新增 |

注意 `snapshotEvents` / `eventAt` / `ownEvents` 这套接口 0.1.2 引入、0.1.6 就弃用了 —— 中间只隔一个月。追上游是在追一个快速移动的目标，这本身是要写进决策依据的事实。

## 已测到的具体差异

把两个 tag 都浅克隆下来（各 90M / 147M），用仓库自带的 `scripts/sync_upstream_catalog.py` 生成兼容目录：

**生成器在最新上游直接失败：**

```
FileNotFoundError: packages/host/apiproxy/src/api/rpc-map.ts
```

`packages/host/apiproxy` 这个包在上游已被整个删除，`rpc-map.ts` 全树找不到。我们那 52 个固定 RPC 的兼容矩阵，就是从这一个文件生成出来的。新结构是：

```
packages/api/gateway              stream-protocol, remote-error-codes
packages/api/remotes              remote-events
packages/api/session-controller
packages/api/workspace-controller
packages/api/terminal-controller
packages/api/workspace-files
packages/api/settings-controller
```

**方法名还在，只是换了形态。** 一开始按带引号的字面量搜 `"session.list"` 得到 0 命中，据此说"52 个全没了"是错的 —— 名字现在是点号属性路径（`methods.agent.session.list`），不再是一张扁平字符串表。按裸子串重新测：

```
52 个固定 RPC 名称中
  仍能在最新树里找到：46
  找不到：            6
    host.listDirectory
    goal.create / goal.pause / goal.resume / goal.complete / goal.clear
```

这个测法有噪声（子串会命中同名前缀），所以"6 个消失"是线索不是定论。但方向明确：**契约表面大体保留，结构被重写了**。

## 对 bench 的影响

1. 现有配对结果照旧有效，但必须写明对比的是 rc.7，不能写成"官方 harness"。
2. 单步上限、上下文窗口这些参数是按 rc.7 / rc.8 的行为测出来的（见 T16、T18）。
3. 若要回答"该用哪个"，需要一条跑**当前上游**的臂。`0.1.5rc1` 已可从 PyPI 获取，uv 缓存也已预热，容器内可离线安装；适配器通过 `inspect.signature` 自适应 Config 变化（rc7 用 `session_root`，0.1.5 用 `dsh_home`），所以加这条臂不需要改适配器。

**尚未做**：拿 0.1.5rc1 或 0.1.6-alpha.1 实际跑一轮。在那之前，任何"XHarness 对官方"的结论都只对 rc.7 成立。