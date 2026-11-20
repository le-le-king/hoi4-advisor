# dll/

这里只有两个文件，别再从 `src/hoi4_bridge/dll/` 拿 DLL 用。

| 文件 | 说明 |
|---|---|
| `neko_hoi4_bridge.c` | 桥接源码（v2 协议：端口自适应 + 无状态管道） |
| `neko_hoi4_bridge.dll` | 由上面这份源码编译出来的产物，插件运行时注入游戏 |

## 为什么单独放一份，不引用 src/hoi4_bridge/

`src/hoi4_bridge/dll/` 里有两个同名不同协议的 DLL：

- `neko_bridge_v2.dll` —— 端口自适应，`PING` 回 `OK pong <port>`（**客户端认这个**）
- `neko_hoi4_bridge.dll` —— 老版，固定 7788，`PING` 只回 `OK pong`（两段，客户端会拒认）

名字和协议是交叉的，很容易拿错。所以插件包里只放一份，源码和产物同名，不给自己留歧义。

## 重新编译

机器上没有 gcc/cl，用 zig：

```bash
Q:/neko开发/.venv/Lib/site-packages/ziglang/zig.exe cc \
  -target x86_64-windows-gnu -shared -O2 \
  -o neko_hoi4_bridge.dll neko_hoi4_bridge.c -lws2_32
```

编译后会顺带产生 `.lib` / `.pdb`，**不要打进去**（1 MB，没用的调试符号）。

## 不开游戏怎么验证 DLL 是对的

把它加载进任意 Python 进程，端口会起来：

```python
import ctypes, socket, time
ctypes.WinDLL(r"Q:/neko开发/src/hoi4_advisor/dll/neko_hoi4_bridge.dll")
time.sleep(0.6)
s = socket.create_connection(("127.0.0.1", 7788), timeout=1)
s.sendall(b"PING\n")
print(s.recv(256))     # b'OK pong 7788\n'
```

看到 `OK pong 7788`（三段）就是对的那一版。`_selftest_advisor.py` 的第 11 项跑的就是这个。
