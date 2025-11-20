from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse


router = APIRouter(prefix="/docs", tags=["Documentation"])


def get_project_root() -> Path:
    """获取项目根目录路径"""
    # 从当前文件位置向上查找，直到找到包含pyproject.toml的目录
    current = Path(__file__).resolve()
    while current.parent != current:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    # 如果找不到，使用相对路径
    return Path(__file__).resolve().parent.parent.parent.parent


@router.get("/documentation", response_class=PlainTextResponse)
async def get_api_documentation():
    """
    获取API文档内容 (README.md)
    """
    try:
        project_root = get_project_root()
        docs_path = project_root / "README.md"

        if not docs_path.exists():
            raise HTTPException(
                status_code=404,
                detail="API documentation file not found"
            )

        with open(docs_path, "r", encoding="utf-8") as f:
            content = f.read()

        return content
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error reading API documentation: {str(e)}"
        )


@router.get("/pyproject", response_class=PlainTextResponse)
async def get_pyproject():
    """
    获取pyproject.toml文件内容
    """
    try:
        project_root = get_project_root()
        pyproject_path = project_root / "pyproject.toml"

        if not pyproject_path.exists():
            raise HTTPException(
                status_code=404,
                detail="pyproject.toml file not found"
            )

        with open(pyproject_path, "r", encoding="utf-8") as f:
            content = f.read()

        return content
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error reading pyproject.toml: {str(e)}"
        )
