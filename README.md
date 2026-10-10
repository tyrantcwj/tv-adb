# tv-adb

网页版安卓设备遥控台：顶部输入设备 IP 连接，右侧实时画面（鼠标/触摸直接操作），左侧遥控器按键。适合电视、盒子、平板这类开了网络 ADB 的设备。

- 画面：scrcpy-server v3.3.4 推 H.264 流，浏览器端在 HTTPS/localhost 下用 WebCodecs，普通 http 局域网地址下自动走 MSE（JMuxer）
- 操作：左键点按/拖动、右键返回、中键主页、滚轮滚动；画面获得焦点后键盘方向键/回车/Esc/退格可用，Ctrl+V 粘贴文字（支持中文）
- 遥控：电源（按住=长按）、熄屏投屏、方向键/OK、返回/主页/菜单/多任务/通知/设置、音量、媒体键、文字输入、截图、安装 APK、旋转、重启、关机
- 设备管理：保存设备并命名，列表存在服务端，容器重启后自动重连；手动「断开」后不再自动重连，把设备的 adb 让给别的主机
- 兼容厂商魔改系统：硬件编码器起不来时自动换软件编码器；像海信 VIDAA 这种连系统 MediaCodec 都不让 shell 用的，自动改用设备自带的 screenrecord 出画面（每 3 分钟会自动重连一次，画面顿一下），选中的方式按设备记住；启动失败的完整日志可在 `/api/diag?serial=<设备>` 查看
- 省设备资源：同一设备只保留一路画面（新窗口接管旧窗口），页面切到后台 15 秒自动停止投屏、回来自动恢复；看端跟不上时服务端丢帧追实时，不积压延迟

## 部署

```yaml
services:
  tv-adb:
    image: ghcr.io/tyrantcwj/tv-adb:latest
    container_name: tv-adb
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      - PASSWORD=
    volumes:
      - ./data:/data
      - ./adbkeys:/root/.android
```

```bash
docker compose up -d
```

访问 `http://<主机IP>:8765`。镜像支持 amd64 / arm64。

| 环境变量 | 说明 |
| --- | --- |
| `PORT` | 监听端口，默认 8765 |
| `PASSWORD` | 设置后启用 HTTP Basic 认证（用户名任意） |

卷：
- `/data`：保存的设备列表
- `/root/.android`：adb 密钥，**务必持久化**，否则每次重建容器设备都要重新授权

自己构建镜像：

```bash
docker build -t tv-adb .
```

## 群晖套件（不需要 Docker）

在 [Releases](https://github.com/tyrantcwj/tv-adb/releases) 下载 `tv-adb-*-x86_64.spk`，套件中心 → 手动安装。

- 要求 DSM 7.0 及以上、x86_64 机型（DS918+ / DS921+ / DS1819+ / DVA1622 / DVA3221 等）
- 套件自带 Python 和 adb，不依赖其他套件；安装向导里可设置访问密码
- 装好后主菜单有 TV ADB 图标，或直接访问 `http://<NAS IP>:8765`
- 数据和 adb 密钥在 `/var/packages/tv-adb/var`，升级保留；内部 adb 服务用 15037 端口，不和 NAS 上其他 adb 冲突

自己打包：`python synology/build.py`，产物在 `dist/`。

## 设备准备

设备需开启网络 ADB（开发者选项里的 USB 调试 / 无线调试，或用数据线执行 `adb tcpip 5555`）。首次连接时设备上会弹出授权框，勾选「一律允许」后在网页上点「重新连接」即可。

## 本地运行

```bash
pip install -r requirements.txt
python app.py
```

需要系统里有 `adb`，或用环境变量 `ADB` 指定路径。
