# main.py - 糟糕的入口执行程序
from services import UserManagementService

if __name__ == "__main__":
    service = UserManagementService()
    admins = service.get_admin_users()
    print("Found administrators:")
    for admin in admins:
        print("- " + admin)
