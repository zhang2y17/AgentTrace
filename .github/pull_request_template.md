## 改动

说明具体问题，以及合并后可以观察到的行为。

## 验证

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `python -m pytest tests -q`
- [ ] `python scripts/verify_contract.py`
- [ ] 若修改评测或 Agent：`python scripts/run_eval.py --dataset doc_research_v1 --gate`

## 契约与数据

- [ ] 已同步相关契约文档和 `docs/contract.lock.json`，或本次不影响契约。
- [ ] 新增样例与评测数据来自公开或合成数据，并已在描述中说明来源。
- [ ] 不包含密钥、生产数据、个人数据、内部接口或无法验证的效果声明。
