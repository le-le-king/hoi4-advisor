# 编译进程内代理（neko_hoi4_agent.dll）

代理是一个普通的 Windows DLL，导出一个 `DllMain`：被注入后起一个监听线程，
在 `127.0.0.1:7801` 上接受文本协议命令（`PING` / `RD` / `WR` / `CALL` / `SCANALL` …）。
它只跟 `hoi4.exe` 打交道，不联网。

## 依赖

只需 [zig](https://ziglang.org/download/)（自带 clang 和 Windows 头文件/库，不用装 MSVC 或 Windows SDK）。

```bash
zig version        # 0.13 及以上都行
```

## 编译

在 `dll/` 目录里：

```bash
zig cc -target x86_64-windows-gnu -shared -O2 \
    -o neko_hoi4_agent.dll \
    neko_hoi4_agent.c \
    -lws2_32 -luser32
```

产物就是 `neko_hoi4_agent.dll`（约 190 KB）。`neko_hoi4_bridge.dll` 同理：

```bash
zig cc -target x86_64-windows-gnu -shared -O2 -o neko_hoi4_bridge.dll neko_hoi4_bridge.c -lws2_32 -luser32
```

编译完把 DLL 放到 `dll/` 下，插件自带的是同名的预编译版本；重新打包即可生效。

## 协议速查

| 命令 | 作用 |
|---|---|
| `PING` | 存活 + 返回 `pid` 和 exe 基址 |
| `VER` | 代理版本和构建时间 |
| `RD <addr> <n>` | 读 n 字节，回 hex |
| `WR <addr> <hex>` | 写，回写入字节数 |
| `CALL <fn> <a1> <a2> <a3> <a4>` | 调用引擎函数（x64 调用约定），回返回值 |
| `SCANALL <value>` | 全地址空间扫一个 8 字节值（特征是 vtable 指针），回命中地址列表 |
| `BPOFF` / `HWBPOFF` | 断开前摘掉所有断点（安全网） |

几条硬约束（都踩过坑）：

- **`SCANALL` 必须用 `memchr` 全速扫**，逐块 `ReadProcessMemory` 复制会慢 3~5 倍，撞穿宿主的 30 秒看门狗。
- 扫内存必须用 **VEH + setjmp** 兜住越界读，并且把 guard **绑到线程 id**——VEH 是进程级的，不绑线程会把别处 `CALL` 的 handler 抢走。
- 写内存前要校验 exe 大小与偏移表匹配，对不上就只读。

## 为什么不用 MSVC

不是偏好问题：这套偏移是从 **1.16.9** 的 exe 逆出来的，编译器的差异会影响不到结果，
但 zig 的 `x64-windows-gnu` 目标能在没有任何 SDK 的机器上一条命令出包，迭代快得多。
