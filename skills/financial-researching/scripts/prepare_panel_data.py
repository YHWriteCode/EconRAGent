"""
prepare_panel_data.py
=====================
职责：将标准化行情数据与基本面数据整合为面板格式，供 linearmodels 使用。

用法：
    python prepare_panel_data.py --market output/market_data.csv --output output/panel_data.csv

依赖：pandas
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


def load_market_data(filepath: str) -> pd.DataFrame:
    """加载标准化行情数据"""
    if not os.path.exists(filepath):
        print(f"错误：文件不存在 - {filepath}")
        sys.exit(1)

    df = pd.read_csv(filepath, parse_dates=["date"])
    required_cols = ["date", "code", "close", "return"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(f"错误：缺少必要列 - {missing}")
        sys.exit(1)

    print(f"已加载行情数据：{df.shape[0]} 行, {df['code'].nunique()} 只股票")
    return df


def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算基础因子。

    当前实现的因子：
    - size_factor: 近20日平均成交额的对数（作为规模代理变量）
    - momentum: 过去20日累计收益率
    - volatility: 过去20日收益率标准差
    - turnover_avg: 近20日平均换手率（如有该列）

    注意：这是 MVP 实现，实际研究中因子应根据研究问题选择。
    """
    df = df.sort_values(["code", "date"]).copy()

    # 规模因子：用成交额的对数作为代理
    if "amount" in df.columns:
        rolling_amount = df.groupby("code")["amount"].transform(
            lambda x: x.rolling(window=20, min_periods=5).mean()
        )
        df["size_factor"] = rolling_amount.apply(
            lambda x: pd.np.log(x) if x > 0 else pd.np.nan
        )
    else:
        # 如果没有成交额列，用收盘价的对数代替
        df["size_factor"] = df["close"].apply(
            lambda x: pd.np.log(x) if x > 0 else pd.np.nan
        )

    # 动量因子：过去 20 日累计收益
    df["momentum"] = df.groupby("code")["return"].transform(
        lambda x: x.rolling(window=20, min_periods=5).sum()
    )

    # 波动率因子：过去 20 日收益率标准差
    df["volatility"] = df.groupby("code")["return"].transform(
        lambda x: x.rolling(window=20, min_periods=5).std()
    )

    return df


def build_panel(df: pd.DataFrame) -> pd.DataFrame:
    """
    将 DataFrame 转换为面板格式（MultiIndex: code, date）。

    步骤：
    1. 确保 code 为字符串
    2. 删除因子列的 NaN
    3. 设置 MultiIndex
    4. 排序与去重
    """
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])

    # 选择面板需要的列
    panel_cols = ["date", "code", "return", "close", "volume"]
    factor_cols = ["size_factor", "momentum", "volatility"]
    available_factors = [c for c in factor_cols if c in df.columns]
    keep_cols = panel_cols + available_factors
    available_keep = [c for c in keep_cols if c in df.columns]
    df = df[available_keep].copy()

    # 删除包含 NaN 的行（因子计算初期的空值）
    before_count = len(df)
    df = df.dropna()
    after_count = len(df)
    if before_count > after_count:
        print(f"已删除 {before_count - after_count} 行含 NaN 的记录")

    # 去除重复
    df = df.drop_duplicates(subset=["code", "date"])

    # 设置 MultiIndex
    df = df.set_index(["code", "date"]).sort_index()

    # 验证
    assert isinstance(df.index, pd.MultiIndex), "索引必须是 MultiIndex"
    assert not df.index.duplicated().any(), "存在重复索引"

    return df


def main():
    parser = argparse.ArgumentParser(description="将行情数据转换为面板格式")
    parser.add_argument("--market", type=str, required=True,
                        help="标准化行情数据 CSV 路径")
    parser.add_argument("--output", type=str, default="output/panel_data.csv",
                        help="输出面板数据 CSV 路径")
    args = parser.parse_args()

    # 加载数据
    df = load_market_data(args.market)

    # 计算因子
    print("正在计算因子...")
    df = compute_factors(df)
    print(f"因子列：{[c for c in df.columns if c in ['size_factor', 'momentum', 'volatility']]}")

    # 构建面板
    print("正在构建面板格式...")
    panel = build_panel(df)

    # 输出
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(output_path, encoding="utf-8-sig")
    print(f"\n面板数据已保存至 {output_path}")
    print(f"面板形状：{panel.shape}")
    print(f"个体数量（code）：{panel.index.get_level_values('code').nunique()}")
    print(f"时间跨度（date）：{panel.index.get_level_values('date').min()} ~ "
          f"{panel.index.get_level_values('date').max()}")
    print(f"\n前 5 行：")
    print(panel.head())


if __name__ == "__main__":
    main()
