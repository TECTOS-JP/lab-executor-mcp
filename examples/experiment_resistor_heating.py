"""
抵抗の自己発熱特性測定実験（visa-mcp 使用例）

直流電源 (Kikusui PMX35-3A) で 100Ω 1/4W 抵抗に電力を投入し、
K型熱電対 + 温度計 (Yokogawa 7563) で温度上昇を記録する。

得られるもの:
- 熱抵抗 Rθ [℃/W]
- 環境温度の外挿値
- 冷却時定数 τ

実行前に: 自分の機器のリソース文字列に PMX / TMC を書き換えてください。
リソース文字列は python -c "import pyvisa; print(pyvisa.ResourceManager().list_resources())" で確認できます。

実行: python examples/experiment_resistor_heating.py
"""
import asyncio
import time
import csv
import re
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from lab_visa_mcp.visa_manager import VisaManager

# ===== 設定: 環境に合わせて書き換えてください =====
PMX = "USB0::0x0B3E::0x1029::<SERIAL>::INSTR"   # Kikusui PMX (USB) — シリアル番号は実機のものに
TMC = "GPIB0::<ADDR>::INSTR"                     # Yokogawa 7563 (GPIB) — アドレスは実機のものに

# 試験ステップ: (印加電圧V, 安定待ち時間秒)
STEPS = [
    (0.0, 30),    # T0 ベースライン
    (1.0, 90),
    (2.0, 90),
    (3.0, 90),
    (4.0, 120),
    (4.5, 120),
    (0.0, 180),   # 冷却
]

LOG_DIR = Path(__file__).parent.parent / "results"
LOG_PATH = LOG_DIR / f"heating_{datetime.now():%Y%m%d_%H%M%S}.csv"


def parse_7563(resp: str) -> float | None:
    """Yokogawa 7563 のデータ応答 'NTKC+00027.2E+0' から温度値を抽出。"""
    m = re.search(r'([+-]\d+\.\d+E[+-]\d+)', resp)
    return float(m.group(1)) if m else None


async def read_temperature(visa: VisaManager) -> float | None:
    """7563 はトーカ指定で測定データを送出するため read のみ。"""
    try:
        resp = await visa.query(TMC, "", timeout_ms=3000)
        return parse_7563(resp)
    except Exception as e:
        print(f"  温度読み出しエラー: {e}")
        return None


async def main():
    visa = VisaManager()
    LOG_DIR.mkdir(exist_ok=True)

    log = []
    print(f"=== 抵抗自己発熱実験 開始 ({datetime.now():%H:%M:%S}) ===")
    print(f"  PMX:  {PMX}")
    print(f"  TMC:  {TMC}")
    print(f"  ログ: {LOG_PATH}\n")

    # 安全のため最初に出力 OFF + 保護設定
    await visa.write(PMX, "OUTP OFF")
    await visa.write(PMX, "VOLT 0")
    await visa.write(PMX, "VOLT:PROT 5.0")    # OVP 5V
    await visa.write(PMX, "CURR:PROT 0.5")    # OCP 500mA (最小値以上)
    await visa.write(PMX, "CURR 0.06")        # CC リミット 60mA

    start_time = time.time()

    for step_idx, (v_set, wait_s) in enumerate(STEPS):
        if step_idx == 0:
            step_label = "T0 baseline"
        elif v_set == 0:
            step_label = "冷却"
        else:
            step_label = f"V={v_set}V"
        print(f"\n[Step {step_idx}] {step_label} ({wait_s}s)")

        if v_set > 0:
            await visa.write(PMX, f"VOLT {v_set}")
            await visa.write(PMX, "OUTP ON")
        else:
            await visa.write(PMX, "OUTP OFF")
            await visa.write(PMX, "VOLT 0")

        deadline = time.time() + wait_s

        while time.time() < deadline:
            t_elapsed = time.time() - start_time
            temp = await read_temperature(visa)

            v_meas = i_meas = None
            if v_set > 0:
                try:
                    v_meas = float((await visa.query(PMX, "MEAS:VOLT?")).strip())
                    i_meas = float((await visa.query(PMX, "MEAS:CURR?")).strip())
                except Exception:
                    pass
            p_meas = (v_meas * i_meas) if (v_meas and i_meas) else None

            entry = {
                "t_s": round(t_elapsed, 1),
                "step": step_idx,
                "v_set": v_set,
                "v_meas": v_meas,
                "i_meas": i_meas,
                "p_meas": p_meas,
                "temp_c": temp,
            }
            log.append(entry)
            print(f"  t={t_elapsed:6.1f}s  V={v_meas}  I={i_meas}  P={p_meas}  T={temp}℃")
            await asyncio.sleep(10)

    # 安全停止
    await visa.write(PMX, "OUTP OFF")
    await visa.write(PMX, "VOLT 0")

    # CSV 書き出し
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["t_s", "step", "v_set", "v_meas", "i_meas", "p_meas", "temp_c"]
        )
        w.writeheader()
        w.writerows(log)

    print(f"\n=== 完了 ({datetime.now():%H:%M:%S}) ===")
    print(f"  ログ保存: {LOG_PATH}")
    print(f"  全 {len(log)} サンプル")
    print(f"\n結果分析: python examples/analyze_heating.py")


if __name__ == "__main__":
    asyncio.run(main())
