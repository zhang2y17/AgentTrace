"""数据库初始化脚本。

用法::

    python scripts/init_db.py            # 建表（幂等）
    python scripts/init_db.py --drop     # 先删表再建（会丢数据，需二次确认）
    python scripts/init_db.py --check    # 只检查连通性

契约：本项目不使用 Alembic（见 DATA_MODEL §5 已知限制）。
字段变更需要 `--drop` 重建，届时请先备份。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import (  # noqa: E402
    check_database_health,
    create_all_tables,
    dispose_engine,
    drop_all_tables,
    get_engine,
)

logger = get_logger("scripts.init_db")


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 AgentTrace 数据库")
    parser.add_argument("--drop", action="store_true", help="先删除所有表再重建（会丢数据）")
    parser.add_argument("--check", action="store_true", help="只检查数据库连通性")
    parser.add_argument("--yes", action="store_true", help="跳过 --drop 的确认提示")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, force=True)

    print("=" * 74)
    print("AgentTrace 数据库初始化")
    print("=" * 74)
    print(f"方言     : {settings.database_dialect}")
    print(f"连接串   : {settings.safe_database_url()}")
    print()

    # ------------------------------------------------------------ 连通性检查
    healthy, error = check_database_health(settings)
    if not healthy:
        print("[失败] 无法连接数据库。")
        print(f"       错误: {error}")
        print()
        print("排查建议：")
        print("  1. PostgreSQL 是否已启动？  docker compose up -d postgres")
        print("  2. DATABASE_URL 是否正确？  当前值见上方'连接串'（口令已脱敏）")
        print("  3. 用 SQLite 快速验证？    设置 DATABASE_URL=sqlite+pysqlite:///./agenttrace.db")
        return 2

    print("[通过] 数据库连通。")
    print()

    if args.check:
        print("仅做连通性检查，未修改任何表。")
        dispose_engine()
        return 0

    # ------------------------------------------------------------ 删表确认
    if args.drop and not args.yes:
        print("[警告] --drop 会删除所有表及其数据，此操作不可逆。")
        answer = input("请输入 'yes' 确认继续: ").strip().lower()
        if answer != "yes":
            print("已取消。")
            dispose_engine()
            return 1

    if args.drop:
        print("正在删除所有表 ...")
        drop_all_tables(get_engine())
        print("[完成] 已删除所有表。")
        print()

    # ------------------------------------------------------------ 建表
    print("正在建表 ...")
    create_all_tables(get_engine())

    import app.db.models  # noqa: F401  —— 确保模型已注册

    tables = sorted(Base.metadata.tables)
    print(f"[完成] 已创建 {len(tables)} 张表：")
    for table in tables:
        print(f"        - {table}")
    print()

    print("下一步：")
    print("  python scripts/seed_data.py      # 播种样例数据与评测集")
    print("  uvicorn app.main:app --reload    # 启动 API")
    print()

    dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(main())
