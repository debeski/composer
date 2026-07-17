from typing import List, Optional


def parse_service_list(raw: Optional[str]) -> List[str]:
    names: List[str] = []
    for item in str(raw or "").replace(",", " ").split():
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names


def join_service_list(names: List[str]) -> str:
    return ",".join(name for name in names if name)
