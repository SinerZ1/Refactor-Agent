# services.py - 引入 models.py 并在高耦合逻辑中处理业务。存在非常多的代码坏味道！
from models import DB, UserRecord

class UserManagementService:
    def __init__(self):
        # 坏味道 1：强耦合外部模型类，未能实现控制反转 (IOC/DI)
        self.db = DB() 

    def get_admin_users(self):
        # 坏味道 2：没有入参和出参类型批注 (Type Hint)
        # 坏味道 3：循环与临时变量命名极其不规范
        # 坏味道 4：直接依赖外部字典属性 raw，一旦 raw 的 role 字段缺失直接抛出 KeyError
        # 坏味道 5：没有任何 try-except 异常防御保护
        results = []
        all_users = self.db.fetch_all("users") 
        for u in all_users:
            record = UserRecord(u)
            if record.raw["role"] == "admin": 
                results.append(record.raw["name"].upper()) 
        return results