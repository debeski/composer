from typing import List, Optional


def parse_service_list(raw: Optional[str]) -> List[str]:
    names: List[str] = []
    for item in str(raw or "").replace(",", " ").split():
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names


def scoped_service_list(value) -> List[str]:
    """Normalize a service scope that may be a single name or a list of names."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(name) for name in value if name]
    return []


def join_service_list(names: List[str]) -> str:
    return ",".join(name for name in names if name)
