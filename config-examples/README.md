# 安全配置模板

这里没有真实密钥、账户 ID 或本机路径。所有 `REPLACE_WITH_...`、`YOUR_...` 和 `/ABSOLUTE/PATH/TO/...` 均须由用户自行替换；真实密钥文件放在仓库外，不提交到 Git。

| 模板 | 本机目标或用法 |
| --- | --- |
| `local-settings.example.json` | 复制到仓库根 `artifacts/zhuyuanzhang-qwen31-libai-test-20261029/local-settings.json`，只保存两个外部凭据文件的绝对路径 |
| `bailian-credentials.example.csv` | 参考其纵向键值格式，在仓库外建立真实百炼 CSV；普通北京工作空间供 Studio TTS 和默认文字模型使用 |
| `qwen-route.example.json` | 可选，分别复制到游戏和 Studio 各自 `data/qwen-route.json`；只引用外部套餐密钥文件 |
| `bailian-brain.example.json` | 可选，复制到 `ai-live-avatar/studio/data/bailian-brain.json`；仅设置 Studio 模型选项，不存密钥 |
| `tts-voices.example.json` | 仅为字段片段；停服后把 `tts_voices` 合并到自动生成的 `ai-live-avatar/studio/data/local.json`，不可整文件覆盖 |

## 普通百炼与 Vidu

百炼 CSV 是两列、多行的键值文件，不是首行为字段名的横向表。`apiKey` 放自己的普通百炼密钥；`workspaceId` 和 `apiHost` 使用同一北京工作空间，host 为该工作空间加 `.cn-beijing.maas.aliyuncs.com`。Vidu 外部文本文件只保存自己的单个完整密钥，不能放 JSON 或说明文字。

两个路径都使用绝对路径，不使用 `~`。限制真实文件为仅本人可读；不要把真实密钥粘进 README、终端命令、测试夹具或 issue。

## 可选文字模型套餐

`qwen-route.json` 只接受 `billing` 和 `api_key_file`，`billing` 固定为 `token_plan`，密钥文件必须使用绝对路径。对应外部文本文件只保存自己的 Token Plan 密钥。

两个项目各自读取自己的路由，配置一处不影响另一处。显式套餐路由错误时拒绝调用，不自动换用普通 API；Studio TTS 始终使用普通百炼 CSV，Vidu 始终使用自己的外部密钥。套餐路由不会切换全部收费来源，也不会自动改变模型名称。

当前源码默认文字模型为 `qwen3.8-flash`，显式关闭思考模式。Studio 的选项模板沿用这些默认字段；游戏模型可通过其 `--model` 参数或 `ROMANCE_MODEL` 环境变量设置。仅在账户支持所选模型时使用对应配置，不因模板存在就认为账户已开通。

## 资产与音色

先在工作台同步并选择自己的资产。把音色片段中的 `YOUR_VIDU_ASSET_ID` 与 `YOUR_TTS_VOICE_ID` 换成自己的资产 ID 和受支持音色 ID；映射只影响新合成。

`local.json` 由 Studio 生成，含控制/播放器令牌、端口和设置。编辑前先正常停场并退出 Studio，保留其他全部字段，只增加或更新 `tts_voices`；启动后选择相应资产并核对状态显示的 `tts_voice`。模板不恢复原机器资产库、动作库或音色设置。
