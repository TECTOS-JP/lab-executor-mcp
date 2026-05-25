"""抵抗自己発熱実験の結果分析"""
import csv
import math
from pathlib import Path

# 直近の結果ファイル
results_dir = Path(__file__).parent.parent / "results"
csv_files = sorted(results_dir.glob("heating_*.csv"))
csv_path = csv_files[-1]
print(f"分析対象: {csv_path.name}\n")

rows = []
with open(csv_path, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        rows.append(r)

# 各ステップの最後3サンプル平均（安定値）
steps = {}
for r in rows:
    s = int(r["step"])
    steps.setdefault(s, []).append(r)

print("=" * 70)
print("各ステップの定常値（最後3サンプル平均）")
print("=" * 70)
print(f"{'Step':>4} {'V_set':>7} {'V_meas':>8} {'I_meas':>9} {'P_meas':>10} {'T_avg':>8} {'ΔT':>7}")
print(f"{'':>4} {'(V)':>7} {'(V)':>8} {'(A)':>9} {'(W)':>10} {'(℃)':>8} {'(℃)':>7}")
print("-" * 70)

# T0 推定: Step 0 か Step 1 開始時
t0 = float(steps[0][-1]["temp_c"]) if 0 in steps else None
if t0 is None or t0 < 26:
    t0 = float(steps[1][0]["temp_c"]) if 1 in steps else 27.0

step_summary = []
for s in sorted(steps.keys()):
    recent = steps[s][-3:]
    temps = [float(r["temp_c"]) for r in recent if r["temp_c"]]
    vs = [float(r["v_meas"]) for r in recent if r["v_meas"]]
    is_ = [float(r["i_meas"]) for r in recent if r["i_meas"]]
    t_avg = sum(temps) / len(temps) if temps else None
    v_avg = sum(vs) / len(vs) if vs else None
    i_avg = sum(is_) / len(is_) if is_ else None
    p = v_avg * i_avg if (v_avg and i_avg) else 0
    v_set = float(recent[0]["v_set"])
    dt = (t_avg - t0) if t_avg else None

    v_s = f"{v_avg:.3f}" if v_avg else "  --  "
    i_s = f"{i_avg*1000:.1f}mA" if i_avg else "    --"
    p_s = f"{p*1000:.1f}mW" if p else "   0  "
    t_s = f"{t_avg:.2f}" if t_avg else "   --"
    dt_s = f"+{dt:.2f}" if dt and dt >= 0 else f"{dt:.2f}" if dt else "  --"

    print(f"{s:>4} {v_set:>7.2f} {v_s:>8} {i_s:>9} {p_s:>10} {t_s:>8} {dt_s:>7}")
    if 1 <= s <= 5 and v_avg and i_avg and t_avg:
        step_summary.append((p, t_avg, dt))

# === 熱抵抗 Rθ の線形回帰 ===
print()
print("=" * 70)
print("熱抵抗 Rθ の線形回帰  (T = T_amb + Rθ × P)")
print("=" * 70)
xs = [p for p, t, dt in step_summary]
ys = [t for p, t, dt in step_summary]
n = len(xs)
sx = sum(xs); sy = sum(ys); sxy = sum(x*y for x,y in zip(xs,ys)); sx2 = sum(x*x for x in xs)
slope = (n*sxy - sx*sy) / (n*sx2 - sx*sx)
intercept = (sy - slope*sx) / n
ss_res = sum((y - (slope*x + intercept))**2 for x,y in zip(xs,ys))
ss_tot = sum((y - sy/n)**2 for y in ys)
r2 = 1 - ss_res/ss_tot

print(f"  熱抵抗  R_theta = {slope:.2f} degC/W")
print(f"  外挿環境温度 T_amb = {intercept:.2f} degC")
print(f"  決定係数 R^2    = {r2:.6f}")

# === 冷却時の熱時定数 τ ===
print()
print("=" * 70)
print("冷却時の熱時定数 τ")
print("=" * 70)
if 6 in steps:
    cooling = steps[6]
    T_start = float(cooling[0]["temp_c"])
    T_end = float(cooling[-1]["temp_c"])
    print(f"  冷却開始時 T = {T_start:.2f}℃")
    print(f"  180秒後  T = {T_end:.2f}℃")
    # 環境温度はretrenched平均
    T_amb_cool = T_end
    DT0 = T_start - T_amb_cool

    # 複数時刻でτ推定
    print(f"\n  各時刻での 1次系時定数推定（T_amb={T_amb_cool:.2f}℃ 仮定）:")
    print(f"  {'t (s)':>6} {'T (℃)':>8} {'ΔT/ΔT0':>10} {'τ (s)':>8}")
    for r in cooling[1:8]:  # 最初の数点
        t = float(r["t_s"]) - float(cooling[0]["t_s"])
        T = float(r["temp_c"])
        dT = T - T_amb_cool
        if DT0 > 0 and dT > 0.5:
            ratio = dT / DT0
            tau = -t / math.log(ratio) if ratio < 1 else float('inf')
            print(f"  {t:>6.1f} {T:>8.2f} {ratio:>10.3f} {tau:>8.1f}")

# === ASCII プロット ===
print()
print("=" * 70)
print("ASCIIプロット: 電力 → 温度")
print("=" * 70)
T_min, T_max = min(ys + [intercept]), max(ys)
width = 50
for i, (x, y, dt) in enumerate(step_summary):
    bar_len = int((y - T_min) / (T_max - T_min) * width)
    bar = "█" * bar_len
    print(f"  P={x*1000:5.1f}mW  T={y:5.2f}℃  ΔT={dt:+5.2f}℃ |{bar}")

print()
print("=" * 70)
print(f"結論: 100Ω 1/4W抵抗の熱抵抗 ≈ {slope:.0f} ℃/W")
print("=" * 70)
