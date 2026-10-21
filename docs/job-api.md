# 临时任务 API

插件把 API 挂在当前 ComfyUI 服务的 `/turing` 下，不启动额外服务，也不开新端口。
生产环境继续通过现有 Unix socket 和 SSH 转发访问。它沿用 ComfyUI 的访问边界和
`Comfy-User` 用户选择，不额外提供公开网络认证网关。

任务只在本次 ComfyUI 启动期间有效。**每次启动先删除上一次的全部 API 任务，
包括未完成任务和已完成但尚未下载的结果。** 不恢复执行，不自动重试。

## 定义工作流

在现有卡片编辑器中制作并保存可执行卡片模板，继续使用 Canvas Inputs/Outputs
和公开的图片、视频、音频、文字素材桩。无需添加 API 专用节点。

- 参数输入使用端点参数；输入图片、视频和音频可以使用素材位置端点，也可以使用
  IMAGE、VIDEO、AUDIO 值端点。
- 素材位置端点所绑定的桩如果有计算输入，上传素材会替代这次请求中的计算输入；
  未上传时按模板原有依赖规则生成。纯加载桩需要上传对应文件。
- 最终结果通过 Canvas Outputs 导出的素材桩确定。支持多输出和图片批次。
- 每次执行使用独立模板副本，不继承旧项目的素材选择。显式提供的数值参数使用固定值，
  不推进原模板的随机／递增游标。
- 普通 UI 工作流 JSON 或孤立的 API prompt 图不等同于可执行卡片模板。请先在卡片编辑器
  保存执行快照；外部编辑导致快照过期时也需要重新保存。

默认 API 名称使用端点显示名称，实际绑定依赖稳定端点 ID。可以在工作流的
`extra.turing_api` 中定义别名，再通过编辑器保存刷新执行快照：

```json
{
  "inputs": {"source_video": "输入端点ID", "prompt": "提示词端点ID"},
  "outputs": {"result_video": "输出端点ID"}
}
```

名称需要唯一。`GET /turing/templates` 返回当前用户卡片库中的 `.json` 模板名、
输入输出、默认参数及选项。未完成或失效模板返回各自的错误，不阻断其他模板。
接口不返回模型图或 Chat 节点的服务凭据；暴露为普通输入的参数默认值会返回给调用方。

API 模板应使用受信任节点。最终保存统一走素材桩：`SaveImage`、`SaveVideo`、
`VHS_VideoCombine` 等独立输出节点会被拒绝。普通第三方计算节点自行写入外部目录、
启动后台进程或访问外部资源的副作用不受此 API 沙箱保护，需要模板作者移除或适配。

## 提交与下载

| 请求 | 行为 |
|---|---|
| `GET /turing/server` | 启动 ID、API 就绪状态、Manager 重启路由是否存在 |
| `GET /turing/templates` | 模板及接口定义 |
| `POST /turing/jobs` | 提交模板、参数和全部素材，返回 202 和任务 ID |
| `GET /turing/jobs/{id}` | 查询状态、节点／步数进度、耗时与估计、错误、结果列表 |
| `GET /turing/jobs/{id}/outputs/{output_id}` | 下载单个结果，不重新编码 |
| `DELETE /turing/jobs/{id}` | 取消／清理，完成返回 204，正在停止或下载返回 202 |

提交使用 `multipart/form-data`，第一个字段必须是名为 `request` 的 JSON。
`files` 的值引用其他 multipart **字段名**，不依赖文件名或服务器路径。

```json
{
  "template_id": "h3_replace.json",
  "request_id": "ca0ba11d-22d2-4bb7-bb63-1d6474c2aa74",
  "parameters": {"prompt": "替换人物面部", "seed": 42},
  "files": {"source_video": "clip", "reference_image": "face"}
}
```

```bash
curl http://127.0.0.1:18189/turing/jobs \
  -F 'request=<request.json;type=application/json' \
  -F 'clip=@source.mp4' \
  -F 'face=@reference.png'
```

也可以用 `"workflow": "graph"` 替代 `template_id`，并附加
`-F 'graph=@card.json;type=application/json'`。两种方式只能选一种。
`request_id` 可省略；同一启动期间，相同 ID 和请求元数据返回原任务，不会重复入队。
已删除的 ID 不能再次执行；更改素材或参数时使用新 ID。去重不比较重复请求的二进制内容。
可提交 `session_id` 防止旧请求在重启后意外进入新会话。

素材流式落盘；请求 JSON 最大 8 MiB，所有字段内容总和受 ComfyUI
`--max-upload-size` 限制，按实际读取字节累计检查。连续 120 秒收不到下一块上传数据则中止。
此版本不支持上传断点续传，下载中断后可以重新下载。

状态为 `receiving`、`preparing`、`queued`、`running`、`succeeded`、`failed` 或 `deleting`。
结果列表提供 `id`、`name`、批次 `index`、`filename`、`bytes`、`sha256` 和相对下载 `url`。
文字结果使用 UTF-8 `.txt`。HEAD 请求只读取下载头，不表示已下载，也不会触发删除。

## 进度与预计时间

状态查询增加以下字段，原有字段不变；`expires` 仍是结果保留期限。

- `queue.position`：当前下一次原生执行在等待队列中的位置，从 1 开始；
  `ahead` 包括前面的等待项和正在执行的项。尚在编译、执行中或已完成时为 `null`。
  一个任务可能多次入队，这不是整个任务在队列中的永久编号。
- `progress.phase`：`receiving`、`preparing`、`queued`、`executing`、`finalizing`、
  `cleanup`、`succeeded` 或 `failed`。`status=running` 时可能处于原生执行间的排队阶段。
- `progress.active_nodes`：当前运行节点的 `id`、`class_type`、可用的 `title`、
  `elapsed_seconds`，以及节点主动报告时的 `value`、`max`、`fraction`、`eta_seconds`。
  没有进度上报的节点不会伪造步数；并行异步节点可以同时出现在列表中。
- `progress.nodes`：**当前这一次原生执行**已知节点数 `known`、实际完成数 `completed`
  和命中缓存数 `cached`，实际完成数不重复计入缓存。动态扩图会增加已知节点数；
  lazy 分支可能不执行全部已知节点，因此不据此生成工作流完成百分比。
- `progress.executions_started/completed`：本任务累计原生执行次数。依赖和多个输出
  可能产生多次执行，无法提前确定总次数；直接返回已上传素材时也可能无需原生执行。
  `progress.outputs` 单独统计导出素材桩的完成数／总数，不是最终文件数。
- `timing`：`started_at/finished_at` 为 Unix 秒时间戳，`elapsed_seconds` 是从接收任务
  开始的总耗时，`queue_seconds` 是累计等待耗时（包括入队准备和连续执行间的等待），`execution_seconds` 包括原生执行、
  整理结果和清理耗时。完成后耗时冻结，不会随结果保留时间增长。

节点 ETA 使用最近最多 5 个单位的步速，中位数估计剩余时间；忽略首个完成单位之前的
模型加载／编译时间，至少获得两个后续步速样本且波动不超过 2 倍才显示。进度重置或
总步数变化时重新估算；长时间没有进展、原估计已过期时回到 `null`。
这是当前节点剩余时间，不是整个工作流剩余时间。

整个任务的 `timing.eta_seconds` 使用同一用户、同模板、同参数及同输入素材规格的成功
记录估算。至少需要两次记录，取最近最多 5 次的中位数，明显不稳定时不显示。
`eta_scope=processing` 表示不包含未知的后续排队；只有估计可用时才有
`estimated_finish`、`eta_source=history` 和 `confidence=low`。`estimate_status` 为
`estimating`、`available`、`queued`、`overdue`、`complete` 或 `unavailable`。
数据不足、正在排队或原估计已超时时，ETA 为 `null`，成功完成时为 0。
素材内容、模型冷热状态和其他负载仍会影响实际耗时，预计时间不是完成期限。
历史只在当前进程内保留，最多 128 种规格，重启后清空；失败任务不参与学习。

进度采集独立于查询频率，按原生任务 ID 和所属 API 任务归集，不读取其他任务的
节点进度，也不改变普通画布的执行方式。此版本通过状态轮询获取，不新增 WebSocket 服务。

## Python 客户端

客户端位于 `comfyui_turing_utils/job_api/client.py`，只依赖 aiohttp 和标准库，
可以独立复制给调用项目使用，无需安装 ComfyUI 或模型环境。

```python
import asyncio
from comfyui_turing_utils.job_api.client import Client

async def main():
    async with Client("http://127.0.0.1:18189") as client:
        result = await client.run(
            template_id="h3_replace.json",
            parameters={"prompt": "替换人物面部", "seed": 42},
            files={"source_video": "source.mp4", "reference_image": "reference.png"},
            timeout=3600,
            on_progress=lambda state: print(state["progress"], state["timing"]),
        )
        paths = await client.download_all(result["id"], "results")
        print(paths)

asyncio.run(main())
```

`run` 和 `wait` 的 `on_progress` 支持普通函数或异步函数。需要自己控制展示时：

```python
job = await client.submit(template_id="h3_replace.json", files={"source_video": "clip.mp4"})
async for state in client.watch(job["id"], timeout=3600):
    print(state["status"], state["progress"]["active_nodes"], state["timing"]["eta_seconds"])
```

`watch` 会包含最后一次成功／失败／删除中的状态；`wait/run` 在失败或取消时抛出异常。
默认每 0.5 秒查询一次，可以通过 `Client(..., poll_interval=1)` 调整。

`download_all` 先下载到本地 `.partial` 文件，核对大小和 SHA256 后改名，全部成功后
自动 DELETE 服务端任务。下载或校验失败时保留服务端结果，删除本地未完成文件。
重试下载会验证并跳过已经完整保存的同名结果；不会覆盖内容不同的本地文件。
使用 `delete=False` 可以保留服务端结果；之后调用 `await client.delete(job_id)`。
服务重启后查询旧任务会抛出 `ServerRestarted`，不会自动重新提交。

## 文件生命周期与重启

任务存放在所属实例的 `output/.turing-api-jobs/<session>/<job>/`，与普通项目隔离。
此目录由插件创建所有权标记并取得独占文件锁，不能指向符号链接或接管其他非空目录。
同一实例误启动第二个进程时，该 API 不就绪，不会清理第一个进程的任务。

执行成功后先把所有结果固化到独立 `outputs`，再删除上传、中间素材、工作流副本和
任务项目数据库；直通输入和保留原音频的结果也不会依赖被清理的输入文件。
失败则删除任务文件，暂时保留不含素材内容的错误状态。原生队列中属于该 API 任务的
历史记录也会移除，不清空其他任务历史或卸载全局模型。

最终结果默认保留 3600 秒，可用启动环境变量 `TURING_API_RESULT_TTL_SECONDS` 调整。
每 60 秒检查过期和失败的清理；查询／下载不会延长保留时间。DELETE 会等待正在执行的
任务停止或现有下载关闭，再删除整个任务目录。重复 DELETE 幂等返回成功。
文件删除失败会显示 `cleanup_error` 并重试，不会伪装为已完成。

启动时在开放任务请求之前清除全部上一轮会话目录。清理失败会使 API 保持不就绪，
`GET /turing/server` 返回 503 和原因；普通画布、模型目录、模板库不属于清理目标。
任务不跨重启恢复，旧 ID 在新进程中返回 404。

`await client.restart()` 复用 Manager 的 `/manager/reboot`，只发送一次重启请求，
随后等待 `session_id` 改变且 `ready=true`。Manager 返回 403 时直接报告，不绕过其策略。
进程完全卡死、无法处理 HTTP 时仍需 SSH 等外部方式恢复。此 API 不自动安装 Manager，
不增加 systemd 服务，也不会因普通工作流报错自动重启。
