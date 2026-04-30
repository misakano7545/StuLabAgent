# 学生机房管理（教师端 + 学生端 Agent）

基于 Python 的 Windows 机房管理工具，采用 **教师端 + 学生端 Agent** 架构：

- 教师端：`tkinter` 图形界面 + TCP 服务端，展示在线学生机并批量下发命令。
- 学生端 Agent：主动连接教师端，周期心跳，执行命令（改名 / 改 IPv4 / 电源 / 网络限制）。

---

## 功能概览

- 教师端实时展示学生机信息：计算机名、上报网卡 MAC、IPv4、连接地址、最后心跳、在线状态。
- 远程命令：
  - 修改计算机名（成功后自动重启）。
  - 修改默认出口网卡 IPv4（静态 / DHCP，支持 DNS）。
  - 电源操作（关机 / 重启）。
  - 网络限制（黑名单 / 白名单 / 完全断网 / 解除限制）。
- 安全控制：支持 `token` 校验，避免未授权 Agent 接入。
- 可用性：断线自动重连，改 IP 导致的短暂断连可自动恢复。

---

## 环境要求

- 系统：Windows 7 及以上（依赖 `wmic`、`netsh`、PowerShell）。
- Python：
  - Windows 7 建议 Python `3.8.x`
  - Windows 10/11 建议 Python `3.10+`
- 权限：
  - 教师端通常无需管理员权限（但需放行监听端口入站）。
  - 学生端执行改名 / 改 IP / 网络限制时必须管理员权限。
- 可选组件（仅学生端网络限制需要）：
  - `mihomo.exe`（必须与 [mihomo](https://github.com/MetaCubeX/mihomo) 发行版一致）

---

## 项目结构

- `teacher/`：教师端入口、UI、TCP 服务。
- `agent/`：学生端入口、机器标识、改名、网络控制逻辑。
- `common/`：通信协议、配置路径、mihomo 规则 YAML 构建。
- `configs/`：示例配置（`teacher.json`、`agent.json`）。
- `package/`：PyInstaller 构建脚本与 `.spec`。

---

## 快速启动（开发环境）

在仓库根目录执行：

```bat
python -m teacher.main --config configs\teacher.json
```

学生机执行：

```bat
python -m agent.main --config configs\agent.json
```

说明：

- `--config` 支持相对路径和绝对路径。
- 开发模式下，相对路径按仓库根目录解析。
- 需要完全静默运行可用 `pythonw.exe` 启动 Agent。

---

## 配置说明

### 教师端 `configs/teacher.json`

默认示例：

```json
{
  "listen_host": "0.0.0.0",
  "listen_port": 18765,
  "token": "change-me",
  "heartbeat_timeout_sec": 35,
  "yaml_file_path": "configs/config.yaml",
  "network_restrict": {
    "mode": "whitelist",
    "rules": [
      {
        "type": "domain",
        "value": "www.baidu.com"
      }
    ]
  }
}
```

字段说明：

- `listen_host`：监听地址，`0.0.0.0` 表示所有网卡。
- `listen_port`：监听端口（默认 `18765`）。
- `token`：共享口令；为空则不校验（不建议）。
- `heartbeat_timeout_sec`：超过该秒数未收到心跳则标记离线。
- `yaml_file_path`：教师端用于同步到学生机的 YAML 配置文件路径（可相对路径）。
- `network_restrict`：教师端网络限制默认规则配置（用于 UI 初始值）。

### 学生端 `configs/agent.json`

```json
{
  "teacher_host": "192.168.1.100",
  "teacher_port": 18765,
  "token": "change-me",
  "heartbeat_interval_sec": 15
}
```

字段说明：

- `teacher_host`：教师机 IP / 主机名。
- `teacher_port`：教师端监听端口。
- `token`：与教师端保持一致。
- `heartbeat_interval_sec`：心跳间隔（秒，最小会被钳制到 1 秒）。

---

## 网络限制说明（基于 mihomo）

学生端接收网络限制命令后，会生成 `configs/config.yaml` 并尝试启动 `mihomo`。

- 支持模式：
  - `blacklist`：命中规则走 `REJECT`，其余放行。
  - `whitelist`：仅规则内放行，其余 `REJECT`。
  - `block_all`：全部拒绝。
  - `disable`：解除限制并删除本地覆盖配置。
- 二进制查找顺序（学生端）：
  - 项目根目录 `mihomo.exe`（开发运行）
  - 程序同目录 `mihomo.exe`（打包运行）
  - 系统 `PATH` 中的 `mihomo`
- 需要管理员权限，否则会返回“需要管理员权限”。

---

## 网络与会话行为

- Agent 连接后先发送 `register`；认证通过后进入心跳循环。
- 教师端在 `ack.commands` 中下发命令，Agent 执行后回传 `result`。
- 同一设备（同 `machine_id`）重连时，教师端会踢掉旧连接，避免重复会话。
- 改 IP 时连接会短暂中断，Agent 会按退避策略自动重连。

---

## 命令执行细节

### 1) 修改计算机名

- Win7（NT 6.1 及以下）：`wmic computersystem ... call rename`
- Win8+：PowerShell `Rename-Computer`
- 成功后自动计划执行 `shutdown -r -t 1`

### 2) 修改 IPv4

先定位默认出口网卡，依次尝试：

1. WMI `DefaultIPGateway`
2. 若本机存在 `Get-NetRoute` cmdlet：PowerShell 默认路由，再 `Get-NetIPConfiguration`（避免仅按系统版本误判，Win7 兼容模式误报为 6.2+ 时不会误调）
3. WMI `Win32_IP4RouteTable`（默认路由的 `InterfaceIndex` → 网卡名，Win7 不依赖 `IPAddress` 字符串反查）
4. `route print -4` + WMI 反查

再用 `netsh interface ipv4` 应用配置：

- DHCP：地址和 DNS 都切换为 DHCP。
- 静态：支持子网掩码或前缀长度（如 `255.255.255.0` / `24` / `/24`），网关可空。

### 3) 电源操作

- 支持 `shutdown` / `reboot`
- 命令下发后约 1 秒执行

---

## 通信协议

- 帧格式：`4 字节大端长度 + UTF-8 JSON`
- 关键消息类型（`common/protocol.py`）：
  - `register` / `register_ok` / `register_fail`
  - `heartbeat` / `ack`
  - `command_rename_host`
  - `command_set_ipv4`
  - `command_power`
  - `command_network_restrict`
  - `request_config` / `config_response`
  - `result`

---

## 打包（PyInstaller）

安装依赖：

```bat
pip install -r requirements.txt
```

构建：

```bat
package\build.bat
```

输出文件：

- `dist\LabTeacher.exe`
- `dist\LabAgent.exe`

---

## 打包后部署建议

### 教师端目录示例

```text
LabTeacher\
  LabTeacher.exe
  configs\
    teacher.json
    config.yaml
```

### 学生端目录示例

```text
LabAgent\
  LabAgent.exe
  mihomo.exe
  configs\
    agent.json
```

说明：

- EXE 默认优先读取“程序同级 `configs`”中的配置；
- 若同级配置不存在，再回退到打包内置配置；
- `--config` 相对路径在打包后按 EXE 所在目录解析。

---

## CI 构建与发布

仓库包含 GitHub Actions 工作流：

- `win7`（Python 3.8）与 `win10`（Python 3.11）双目标构建；
- 自动下载 `mihomo` 并打包；
- Tag 发布时自动上传 Release 资产。

---

## 常见问题

- 教师端看不到学生机：
  - 检查 `teacher_host`、端口、防火墙策略、`token` 是否一致。
- 命令下发成功但执行失败：
  - 检查学生端是否管理员权限运行。
- 改 IP 后短时离线：
  - 属于预期行为，等待 Agent 自动重连。
- 网络限制无效：
  - 检查 `mihomo.exe` 是否存在、是否管理员运行、`config.yaml` 是否成功写入。

---

## 鸣谢

- [MetaCubeX / mihomo](https://github.com/MetaCubeX/mihomo)