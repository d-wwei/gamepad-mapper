---
name: gamepad-mapper
description: >
  读写并管理本机的游戏手柄→键盘快捷键映射器 (gamepad-mapper)。
  用于：修改手柄按键映射、切换/新建配置方案 (profile)、恢复出厂设置、
  校准或适配新手柄、启动/停止监听、查看状态。
  触发词："改手柄映射"、"手柄按键"、"切换手柄配置"、"恢复手柄出厂"、
  "手柄映射器"、"gamepad mapper"、"remap controller"、"controller profile"。
---

# gamepad-mapper

把游戏手柄的按键映射成 macOS 键盘快捷键。YAML 配置驱动，`run` 运行时改配置会**热重载**，所以你（agent）改完 profile 用户无需重启即可生效。

## 工具位置

```
~/AI-Vault/scripts/gamepad-mapper/
├── mapper.py            # 主程序
├── gamepad-mapper       # wrapper：等价于 .venv/bin/python mapper.py
├── layouts/*.yaml       # 「按键编号→名称」布局，按手柄区分（automap/calibrate 生成）
├── profiles/*.yaml      # 多套映射方案
├── state.json           # 当前激活的 profile
└── .venv/               # pygame + pyyaml
```

所有命令都在该目录下用 wrapper 运行，例如：
`cd ~/AI-Vault/scripts/gamepad-mapper && ./gamepad-mapper status`

## CLI 速查

| 命令 | 作用 |
|---|---|
| `./gamepad-mapper run` | 启动常驻监听（前台）。改 profile 会热重载 |
| `./gamepad-mapper list` | 列出所有 profile，`*` 标记当前激活 |
| `./gamepad-mapper switch <name>` | 切换激活 profile |
| `./gamepad-mapper reset` | 恢复出厂：重写 `default.yaml` 为工厂默认并激活它 |
| `./gamepad-mapper automap` | 从 SDL 数据库自动生成当前手柄布局（无需按键，推荐） |
| `./gamepad-mapper calibrate` | 手动逐键校准（SDL 不认识的手柄用） |
| `./gamepad-mapper status` | 当前 profile、手柄是否连接、是否有匹配布局 |
| `./gamepad-mapper probe` | 打印手柄原始按键/轴编号（调试） |

## profile 文件格式

`profiles/<name>.yaml`：

```yaml
name: coding
description: 编码场景
bindings:
  A: return            # 字符串 = 键盘快捷键
  B: escape
  X: cmd+c
  Y: cmd+v
  ZR: cmd+s
  Plus: cmd+space
  dpad_up: up
  Home:
    action: none       # 字典形式 = 高级动作
```

### 可用按键名（Switch Pro）
`A B X Y`、`L R ZL ZR`、`Minus Plus`、`Home Capture`、`L3 R3`、`dpad_up dpad_down dpad_left dpad_right`。
> 命名按**手柄上印的标签**：印 A 的键就写 `A`。布局已处理任天堂的 A/B、X/Y 位置交换。

### 绑定值的两种写法
1. **快捷键字符串**：`"cmd+shift+4"`、`"return"`、`"up"`、`"a"`
   - 修饰键：`cmd`/`command`、`shift`、`alt`/`option`、`ctrl`/`control`
   - 区分左右修饰键：`rctrl`/`lctrl`、`rshift`/`lshift`、`rcmd`/`ralt` 等（走 Quartz 精确键码，需辅助功能授权）
   - 纯修饰键也能当一个"键"：如 `rctrl`（单独右Ctrl）、`rctrl+rshift`；能否被目标 App（如 typeless）识别取决于该 App 的监听方式
   - 命名键：`return enter tab space delete escape left right up down home end pageup pagedown f1`–`f12`
   - 其他单字符直接写该字符（如 `cmd+c`、`cmd+-`、`cmd+=`）
2. **动作字典** `{action: ...}`：
   - `{action: shortcut, keys: "cmd+c"}` — 同字符串写法
   - `{action: text, value: "hello"}` — 输入一段文字
   - `{action: none}` — 该键不绑定
   - `{action: profile_next}` / `{action: profile_prev}` — 用手柄键在 profile 间循环切换
   - `{action: mouse_click}` / `{action: mouse_rightclick}` — 鼠标左键 / 右键单击（需辅助功能授权）
   - `{action: shell, cmd: "..."}` — 运行 shell 命令（**Layer 2 / agent 钩子**，见末尾）

## 摇杆配置 (sticks)

profile 顶层可加 `sticks` 段，把摇杆作为模拟输入：

```yaml
sticks:
  left:
    mode: mouse        # 左摇杆控制鼠标移动
    speed: 900         # 像素/秒，调灵敏度
    deadzone: 0.15
  right:
    mode: dpad         # 右摇杆按方向发 ↑↓←→
    threshold: 0.6
    repeat: 0.13       # 持续推时的连发间隔(秒)
```

- 左摇杆 `mode: mouse` 用 `CGWarpMouseCursorPosition`，**移动鼠标无需辅助功能授权**。
- 鼠标点击（L3/R3 绑 `mouse_click`/`mouse_rightclick`）用事件注入，**需要辅助功能授权**。
- 摇杆依赖 layout 里的 `axes`（leftx/lefty/rightx/righty）；`automap` 会自动写入。

## 常见任务（agent 操作指南）

- **改某个键**：编辑当前激活 profile（`status` 看是哪个）里对应行。run 在跑就立即生效。
- **新建方案**：复制一个 `profiles/*.yaml`，改 `name`/`description`/`bindings`，再 `switch <name>`。
- **切换方案**：`./gamepad-mapper switch <name>`。
- **恢复出厂**：`./gamepad-mapper reset`（仅重置 `default.yaml`，不动其他 profile）。
- **换了新手柄**：先 `./gamepad-mapper automap`；不行再 `calibrate`。
- **不要手改 `layouts/`**：用 `automap`/`calibrate` 生成；`profiles/` 才是日常编辑对象。

## 注意事项

- **权限**：发按键、鼠标点击需在 *系统设置 → 隐私与安全性 → 辅助功能* 给运行它的终端 App 授权；读手柄若被拦，再到 *输入监控* 授权。**左摇杆控鼠标移动(warp)是例外，无需授权**。
- **停止**：`run` 是前台常驻，按 Ctrl-C 即可干净退出（已处理 SDL 信号拦截问题）。
- **出厂默认不可丢**：工厂默认硬编码在 `mapper.py` 的 `FACTORY_DEFAULT`，`reset` 永远能恢复到可用状态。
- **热重载**：`run` 每 0.5s 检查激活 profile 与 `state.json`，改动自动重载。

## Layer 2：与 agent 工作流结合（预留）

`{action: shell, cmd: "..."}` 可让手柄键触发任意命令，包括 headless 调用 Claude：
```yaml
bindings:
  ZR:
    action: shell
    cmd: 'cd ~/myrepo && claude -p "review 我的 git diff" >> /tmp/agent.log 2>&1'
```
当前出厂 profile 不含 shell 动作（安全）；需要时再加。
