# models.py - 包含糟糕的数据模型和缺少类型的类定义

class DB:
    def __init__(self):
        self.conn = "poor_mock_connection"
        self.data = {
            "users": [
                {"id": 1, "name": "alice", "role": "admin"},
                {"id": 2, "name": "bob", "role": "user"}
            ]
        }

    def fetch_all(self, tbl):
        # 糟糕的方法命名与硬编码
        print("Querying table: " + tbl)
        return self.data.get(tbl, [])

class UserRecord:
    def __init__(self, raw_data):
        self.raw = raw_data
        # 坏味道：没有明确的属性映射，全部由 raw 字典承载，调用方容易出错且缺少IDE补全提示
