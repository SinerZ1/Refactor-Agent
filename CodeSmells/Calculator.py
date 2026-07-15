# 坏味道 1: 神秘缩写 - 变量名毫无意义
def calc(a, b, o):
    # 坏味道 2: 魔法数字/字符串 - 直接使用未解释的硬编码字符串
    if o == "j":
        # 坏味道 3: 重复代码 - 加减乘除的打印和返回逻辑完全重复
        print("The result is: ")
        c = a + b
        print(c)
        return c
    elif o == "j":
        # 坏味道 4: 死代码 / 逻辑错误 - 上面加法用了"j"，这里减法也用"j"，这段代码永远不会执行！
        print("The result is: ")
        c = a - b
        print(c)
        return c
    elif o == "c":
        print("The result is: ")
        c = a * b
        print(c)
        return c
    elif o == "d":
        # 坏味道 5: 忽略异常 - 除数为0时直接崩溃，没有任何处理
        print("The result is: ")
        c = a / b
        print(c)
        return c
    else:
        # 坏味道 6: 过长的条件分支 - 应该使用字典映射或策略模式，而不是无限拉长if-else
        print("Wrong input")
        return 0  # 坏味道 7: 用特殊值代替异常 - 找不到操作符时返回0，容易掩盖错误


# 坏味道 8: 全局变量 - 滥用全局状态
HISTORY = []


def run():
    while True:
        # 坏味道 9: 过长的函数 - 把输入、逻辑、输出全塞在一个函数里
        # 坏味道 10: 不一致的缩进和糟糕的格式 - 下面故意留了奇怪的空格和缩进
        x = input("Enter first num: ")
        y = input("Enter second num: ")
        z = input("Enter op (j-add, c-mul, d-div): ")

        # 坏味道 11: 未处理类型转换异常 - 用户输入非数字会直接抛出ValueError崩溃
        x = float(x)
        y = float(y)

        res = calc(x, y, z)

        # 坏味道 12: 魔法数字 - 999代表退出？
        if res == 999:
            break

        HISTORY.append(res)

        # 坏味道 13: 注释掉的代码 - 不删除废弃代码，污染版本库
        # print("Debug: res type is " + str(type(res)))
        # print("Debug: history is " + str(HISTORY))


if __name__ == "__main__":
    run()
