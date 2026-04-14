"""
run_panel_model.py
==================
职责：读取面板数据，执行面板回归（固定效应），输出回归结果摘要。

用法：
    python run_panel_model.py --input output/panel_data.csv --dep return --indep momentum,volatility,size_factor --output output/regression_summary.txt

依赖：pandas, linearmodels, statsmodels
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

try:
    from linearmodels.panel import PanelOLS, RandomEffects
except ImportError:
    print("错误：未安装 linearmodels，请执行 pip install linearmodels")
    sys.exit(1)

try:
    import statsmodels.api as sm
except ImportError:
    print("错误：未安装 statsmodels，请执行 pip install statsmodels")
    sys.exit(1)


def load_panel(filepath: str) -> pd.DataFrame:
    """
    加载面板数据并验证索引格式。
    要求 CSV 中包含 code 和 date 列（或已作为 MultiIndex 存储）。
    """
    if not os.path.exists(filepath):
        print(f"错误：文件不存在 - {filepath}")
        sys.exit(1)

    df = pd.read_csv(filepath)

    # 如果 code 和 date 是普通列，设置为 MultiIndex
    if "code" in df.columns and "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = df["code"].astype(str).str.zfill(6)
        df = df.set_index(["code", "date"]).sort_index()
    elif not isinstance(df.index, pd.MultiIndex):
        print("错误：数据既没有 code/date 列，也没有 MultiIndex")
        sys.exit(1)

    print(f"面板数据已加载：{df.shape}")
    print(f"  个体数: {df.index.get_level_values(0).nunique()}")
    print(f"  时间数: {df.index.get_level_values(1).nunique()}")
    return df


def run_fixed_effects(df: pd.DataFrame, dep: str, indep: list,
                      entity_effects: bool = True,
                      time_effects: bool = False) -> object:
    """
    执行固定效应面板回归。

    Parameters
    ----------
    df : pd.DataFrame
        面板数据，MultiIndex (code, date)
    dep : str
        因变量列名
    indep : list
        自变量列名列表
    entity_effects : bool
        是否包含个体固定效应
    time_effects : bool
        是否包含时间固定效应

    Returns
    -------
    result : PanelOLS result
    """
    # 验证列存在
    all_vars = [dep] + indep
    missing = [v for v in all_vars if v not in df.columns]
    if missing:
        print(f"错误：以下变量在数据中不存在 - {missing}")
        print(f"可用列：{list(df.columns)}")
        sys.exit(1)

    # 删除回归变量中的 NaN
    subset = df[all_vars].dropna()
    dropped = len(df) - len(subset)
    if dropped > 0:
        print(f"警告：删除了 {dropped} 行包含 NaN 的观测")

    if len(subset) < 30:
        print(f"错误：有效观测数仅 {len(subset)}，不足以执行回归")
        sys.exit(1)

    y = subset[dep]
    X = subset[indep]
    X = sm.add_constant(X)

    print(f"\n回归设定：")
    print(f"  因变量: {dep}")
    print(f"  自变量: {indep}")
    print(f"  个体效应: {entity_effects}")
    print(f"  时间效应: {time_effects}")
    print(f"  有效观测: {len(subset)}")

    model = PanelOLS(
        dependent=y,
        exog=X,
        entity_effects=entity_effects,
        time_effects=time_effects,
    )

    result = model.fit(cov_type="clustered", cluster_entity=True)
    return result


def extract_coefficients(result) -> pd.DataFrame:
    """从回归结果中提取系数表"""
    coef_df = pd.DataFrame({
        "coefficient": result.params,
        "std_error": result.std_errors,
        "t_statistic": result.tstats,
        "p_value": result.pvalues,
    })
    coef_df["significant_5pct"] = coef_df["p_value"] < 0.05
    coef_df["significant_1pct"] = coef_df["p_value"] < 0.01
    return coef_df


def save_results(result, coef_df: pd.DataFrame, output_dir: str):
    """保存回归结果到文件"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 保存完整摘要
    summary_path = output_path / "regression_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(str(result.summary))
    print(f"\n回归摘要已保存至 {summary_path}")

    # 保存系数表
    coef_path = output_path / "coefficients.csv"
    coef_df.to_csv(coef_path, encoding="utf-8-sig")
    print(f"系数表已保存至 {coef_path}")


def main():
    parser = argparse.ArgumentParser(description="执行面板回归")
    parser.add_argument("--input", type=str, required=True,
                        help="面板数据 CSV 路径")
    parser.add_argument("--dep", type=str, default="return",
                        help="因变量列名，默认 return")
    parser.add_argument("--indep", type=str, required=True,
                        help="自变量列名，逗号分隔，如 momentum,volatility,size_factor")
    parser.add_argument("--entity-effects", action="store_true", default=True,
                        help="是否包含个体固定效应（默认是）")
    parser.add_argument("--time-effects", action="store_true", default=False,
                        help="是否包含时间固定效应（默认否）")
    parser.add_argument("--output", type=str, default="output",
                        help="输出目录路径，默认 output")
    args = parser.parse_args()

    indep = [v.strip() for v in args.indep.split(",")]

    # 加载面板数据
    df = load_panel(args.input)

    # 执行回归
    result = run_fixed_effects(
        df, args.dep, indep,
        entity_effects=args.entity_effects,
        time_effects=args.time_effects,
    )

    # 打印结果
    print("\n" + "=" * 60)
    print(result.summary)
    print("=" * 60)

    # 提取系数
    coef_df = extract_coefficients(result)
    print("\n系数表：")
    print(coef_df.to_string())

    # 识别显著因子
    sig_factors = coef_df[coef_df["significant_5pct"]].index.tolist()
    if "const" in sig_factors:
        sig_factors.remove("const")
    print(f"\n在 5% 水平显著的因子：{sig_factors if sig_factors else '无'}")

    # 保存结果
    save_results(result, coef_df, args.output)


if __name__ == "__main__":
    main()
