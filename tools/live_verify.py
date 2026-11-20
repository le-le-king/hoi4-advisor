"""真机验证一条命令（游戏开着的时候跑）。

    python _probe/_p191_live_verify.py

它按顺序做四件事，每步都给出"看到了什么"，最后给结论：

  1. 体检：游戏进程 / 代理注入 / 偏移匹配
  2. 接管状态：托管了哪个集团军、多少师、现在在哪
  3. 画一条进攻线，然后盯 N 轮，**逐轮打印**：推了几个省、有没有在等、还在打哪些
  4. 结论：战线是不是**成片**推进（每轮 >1 个省），还是会卡住

离线验不了的东西就两条，这里都能看出来：
  · 引擎侧战斗结算（打几轮下来）
  · 打完要不要靠跟进指令把部队送进新占的省（看"跟进"那一列）

注意：这会**真的下命令**。跑之前确认你想让它打（它只动你已经接管的师）。
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
pkg = types.ModuleType("hoi4_advisor")
pkg.__path__ = [str(ROOT / "src" / "hoi4_advisor")]
sys.modules["hoi4_advisor"] = pkg

from hoi4_advisor import command, config, livegame  # noqa: E402

try:
    DATA = Path(config.data_dir())
except Exception:  # noqa: BLE001 拿不到就按脚本位置推（插件目录/data）
    DATA = Path(__file__).resolve().parent.parent / "data"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0


def main() -> int:
    print("=" * 70)
    print("一、体检")
    live = livegame.LiveGame(DATA, log=lambda m: print("   [live]", m))
    try:
        ag = live.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 连不上代理：{type(exc).__name__}: {exc}")
        print("    游戏没开 / 代理没注入时就是这句。开游戏后再跑。")
        return 2
    try:
        print(f"  ✓ 代理在：pid={ag.pid} base={ag.base:#x}")
        print(f"  版本：{ag.cmd('VER') if hasattr(ag, 'cmd') else '(略)'}")
    finally:
        ag.close()

    print()
    print("二、接管状态")
    cmd = command.Commander(live, DATA, log=lambda m: print("   [cmd]", m))
    st = cmd._read_state()
    if not st or not st.get("ids"):
        print("  ✗ 现在没接管任何部队。先在插件里 claim 一个集团军，再跑这个脚本。")
        return 2
    print(f"  ✓ {st.get('army')}：{len(st['ids'])} 个师")
    obs = cmd.observe(0, "")
    from collections import Counter
    dist = Counter(int(u["prov"]) for u in obs["units"])
    print(f"  现在分布：{dist.most_common(6)}")

    print()
    print("三、画进攻线")
    print("  目标：请把目标省号填进 OBJECTIVE 常量，或者用下面的前线自动挑一个。")
    print("  （这一步需要你给目标，所以脚本先停在这里，不擅自开打。）")
    print()
    print("  想让它朝某个省推，跑：")
    print("    python _probe/_call.py hoi4_take_command --kv action=offensive \\")
    print("        --kv target=<省号> --kv units=<番号或兵种> --kv country=<国名>")
    print()
    print("  然后每 %d 秒问一次进度，看这四列：" % int(WAIT))
    print("    推=本轮新下的推进令  等=上一格还在打（不重复下令）")
    print("    跟进=把后方部队送上新战线的单  薄=只剩 1 个师、抽不出人的省")
    print()
    print("四、盯 N 轮")
    for i in range(ROUNDS):
        line = ((cmd._read_state().get("plan") or {}).get("offensive") or {})
        if not line:
            print(f"  t={i*int(WAIT):3d}s  没有进攻线（还没画，或者撤了）")
            break
        pend = line.get("pending") or []
        print(f"  t={i*int(WAIT):3d}s  目标 {line.get('target')}  "
              f"已推 {line.get('step', 0)} 格  "
              f"在打 {pend[:6]}{'…' if len(pend) > 6 else ''}")
        if i < ROUNDS - 1:
            time.sleep(WAIT)

    print()
    print("=" * 70)
    print("结论怎么读：")
    print("  · 每轮\"已推\"在涨、\"在打\"那串在换 → 线在走，正常")
    print("  · \"已推\"一直不动、\"在打\"一直是同一串 → 打不下来（兵不够/对面太强），")
    print("    不是命令丢了；补兵或者换目标")
    print("  · 线卡在某几个省不动、也没有\"在打\" → 那几格只剩 1 个师（列进 thin 了），")
    print("    抽不出人推，要往那儿添兵")
    return 0


if __name__ == "__main__":
    sys.exit(main())