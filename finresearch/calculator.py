"""受限 Decimal 算术，不执行 Python 代码或访问文件/网络。"""
import ast
from decimal import Decimal, localcontext


def calculate(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 3000:
        raise ValueError("算式必须为1—3000字符")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 250:
        raise ValueError("算式过于复杂")
    def bounded(value):
        if not value.is_finite() or value.adjusted() > 100 or value.as_tuple().exponent < -200:
            raise ValueError("数字或结果超出允许的大小/小数位范围")
        return value
    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            segment = ast.get_source_segment(expression, node)
            if len(segment) > 64:
                raise ValueError("数字位数过多")
            return bounded(Decimal(segment))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = evaluate(node.operand)
            return bounded(result if isinstance(node.op, ast.UAdd) else -result)
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, ast.Pow) and abs(right) <= 100:
                result = left ** right
            else:
                raise ValueError("仅支持 + - * / **，幂指数绝对值不超过100")
            if not result.is_finite() or abs(result) > Decimal("1e100"):
                raise ValueError("计算结果超出允许范围")
            return bounded(result)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"sum", "min", "max", "abs"} and not node.keywords:
            values = [evaluate(arg) for arg in node.args]
            if not values:
                raise ValueError("函数参数不能为空")
            if node.func.id == "abs" and len(values) == 1:
                return bounded(abs(values[0]))
            if node.func.id == "sum":
                return bounded(sum(values, Decimal(0)))
            if node.func.id == "min":
                return bounded(min(values))
            if node.func.id == "max":
                return bounded(max(values))
        raise ValueError("算式包含不允许的语法")
    with localcontext() as context:
        context.prec = 40
        result = bounded(evaluate(tree))
    return {"expression": expression, "value": format(result, "f"), "precision": "40 significant decimal digits"}
