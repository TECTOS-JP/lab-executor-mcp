"""
VISA 接続確認スクリプト。
visa-mcp サーバーを起動する前に、PyVISA レベルで機器との疎通を確認します。

実行: python examples/verify_connection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main():
    print("=== visa-mcp - VISA 接続確認 ===\n")

    # 1. pyvisa インポート確認
    try:
        import pyvisa
        print(f"[OK] pyvisa {pyvisa.__version__} インポート成功")
    except ImportError as e:
        print(f"[NG] pyvisa インポート失敗: {e}")
        print("     pip install pyvisa を実行してください。")
        return

    # 2. VISA ResourceManager 初期化
    try:
        rm = pyvisa.ResourceManager()
        print(f"[OK] ResourceManager 初期化成功")
        print(f"     VISA ライブラリ: {rm.visalib}")
    except Exception as e:
        print(f"[NG] ResourceManager 初期化失敗: {e}")
        print("     NI-VISA / Keysight IO Libraries / pyvisa-py 等の VISA バックエンドを確認してください。")
        return

    # 3. リソース列挙
    try:
        resources = rm.list_resources()
        print(f"\n[OK] 検出されたリソース: {len(resources)} 件")
        for r in resources:
            print(f"     - {r}")
    except Exception as e:
        print(f"[NG] リソース列挙失敗: {e}")
        return

    if not resources:
        print("\n接続された機器が見つかりません。")
        print("確認: ケーブル接続、電源 ON、GPIB アドレス設定、USB ドライバ。")
        return

    # 4. 各リソースに *IDN? を試行
    print("\n--- *IDN? クエリ結果 ---")
    for resource_name in resources:
        try:
            res = rm.open_resource(resource_name)
            res.timeout = 2000
            idn = res.query("*IDN?")
            res.close()
            print(f"[OK] {resource_name}: {idn.strip()}")
        except Exception as e:
            print(f"[--] {resource_name}: *IDN? 非対応または失敗 ({type(e).__name__})")
            print(f"        → 旧世代非 SCPI 機器の可能性。bind_definition で手動バインドしてください。")

    # 5. YAML 定義ファイルの確認
    for candidate in ["instruments", "examples/instruments"]:
        d = Path(__file__).parent.parent / candidate
        if not d.exists():
            continue
        yaml_files = [f for f in d.glob("*.yaml") if not f.name.startswith("_")]
        if yaml_files:
            print(f"\n--- {candidate}/ YAML 定義ファイル: {len(yaml_files)} 件 ---")
            for f in yaml_files:
                print(f"     - {f.name}")

    print("\n=== 確認完了 ===")


if __name__ == "__main__":
    main()
