"""UI automation: UIAutomator hierarchy dump, element-based interaction, and monkey testing.

Three approaches for UI control, each with different trade-offs:

1. **UIAutomator dump** — structured XML of the entire screen. Works for any
   app, not just browsers. Find elements by text, resource-id, class, or
   content-desc and compute tap coordinates from bounds.

2. **Smart tap** — combines UIAutomator dump with ADB input. Find an element
   by its visible text or resource ID, then tap its centre automatically.

3. **Monkey** — random event generator for stress testing / crash discovery.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from harness_android.adb import ADB

console = Console()


# ── UIAutomator hierarchy ────────────────────────────────────────────


@dataclass
class UIElement:
    """A single node from the UIAutomator view hierarchy."""
    index: int = 0
    text: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    content_desc: str = ""
    checkable: bool = False
    checked: bool = False
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    long_clickable: bool = False
    selected: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    children: list["UIElement"] = field(default_factory=list)

    @property
    def centre(self) -> tuple[int, int]:
        """Centre point of the element bounds."""
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(s: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return (0, 0, 0, 0)


def _parse_node(node: ET.Element) -> UIElement:
    el = UIElement(
        index=int(node.get("index", "0")),
        text=node.get("text", ""),
        resource_id=node.get("resource-id", ""),
        class_name=node.get("class", ""),
        package=node.get("package", ""),
        content_desc=node.get("content-desc", ""),
        checkable=node.get("checkable") == "true",
        checked=node.get("checked") == "true",
        clickable=node.get("clickable") == "true",
        enabled=node.get("enabled") == "true",
        focusable=node.get("focusable") == "true",
        focused=node.get("focused") == "true",
        scrollable=node.get("scrollable") == "true",
        long_clickable=node.get("long-clickable") == "true",
        selected=node.get("selected") == "true",
        bounds=_parse_bounds(node.get("bounds", "")),
    )
    for child in node:
        el.children.append(_parse_node(child))
    return el


def dump_hierarchy(adb: ADB) -> UIElement:
    """Dump the UI hierarchy via ``uiautomator dump`` and return the root element."""
    adb.shell("uiautomator", "dump", "/sdcard/window_dump.xml")
    xml_text = adb.shell("cat", "/sdcard/window_dump.xml")
    root = ET.fromstring(xml_text)
    return _parse_node(root)


def flatten(element: UIElement) -> list[UIElement]:
    """Flatten the hierarchy tree into a list of all elements."""
    result = [element]
    for child in element.children:
        result.extend(flatten(child))
    return result


def find_by_text(root: UIElement, text: str, exact: bool = False) -> list[UIElement]:
    """Find all elements whose text matches (case-insensitive substring or exact)."""
    results = []
    for el in flatten(root):
        if exact:
            if el.text == text:
                results.append(el)
        else:
            if text.lower() in el.text.lower():
                results.append(el)
    return results


def find_by_resource_id(root: UIElement, resource_id: str) -> list[UIElement]:
    """Find all elements whose resource-id contains the given string."""
    return [
        el for el in flatten(root)
        if resource_id in el.resource_id
    ]


def find_by_content_desc(root: UIElement, desc: str) -> list[UIElement]:
    """Find elements by content-desc (accessibility label)."""
    return [
        el for el in flatten(root)
        if desc.lower() in el.content_desc.lower()
    ]


def find_by_class(root: UIElement, class_name: str) -> list[UIElement]:
    """Find elements by class name (e.g. 'android.widget.Button')."""
    return [
        el for el in flatten(root)
        if class_name in el.class_name
    ]


def find_clickable(root: UIElement) -> list[UIElement]:
    """Find all clickable elements."""
    return [el for el in flatten(root) if el.clickable]


# ── Smart tap / type ─────────────────────────────────────────────────


def tap_element(adb: ADB, root: UIElement, text: str) -> bool:
    """Find an element by visible text and tap its centre.

    Returns True if an element was found and tapped.
    """
    matches = find_by_text(root, text)
    if not matches:
        console.print(f"[red]No element found with text: {text!r}")
        return False
    el = matches[0]
    x, y = el.centre
    console.print(
        f"[green]Tapping '{el.text}' at ({x}, {y})"
        f"  [{el.class_name}]"
    )
    adb.tap(x, y)
    return True


def tap_by_resource_id(adb: ADB, root: UIElement, resource_id: str) -> bool:
    """Find an element by resource-id and tap its centre."""
    matches = find_by_resource_id(root, resource_id)
    if not matches:
        console.print(f"[red]No element found with resource-id: {resource_id!r}")
        return False
    el = matches[0]
    x, y = el.centre
    console.print(f"[green]Tapping resource-id '{el.resource_id}' at ({x}, {y})")
    adb.tap(x, y)
    return True


def type_into(adb: ADB, root: UIElement, resource_id: str, text: str) -> bool:
    """Tap a text field by resource-id and type text into it."""
    if not tap_by_resource_id(adb, root, resource_id):
        return False
    import time
    time.sleep(0.3)
    adb.text(text)
    return True


# ── Monkey stress testing ────────────────────────────────────────────


def run_monkey(
    adb: ADB,
    package: str | None = None,
    event_count: int = 5000,
    seed: int | None = None,
    throttle_ms: int = 50,
    categories: list[str] | None = None,
    ignore_crashes: bool = False,
    ignore_timeouts: bool = False,
    verbose: int = 0,
) -> str:
    """Run the ``monkey`` random event generator.

    Args:
        package: Restrict events to this package (recommended).
        event_count: Number of random events to generate.
        seed: Random seed (reproducible runs).
        throttle_ms: Delay between events in milliseconds.
        categories: Intent categories to include.
        ignore_crashes: Keep going after app crashes.
        ignore_timeouts: Keep going after ANR timeouts.
        verbose: Verbosity level (0-3).

    Returns:
        The full monkey output (contains crash reports if any).
    """
    cmd_parts = ["monkey"]

    if package:
        cmd_parts.extend(["-p", package])

    if categories:
        for cat in categories:
            cmd_parts.extend(["-c", cat])

    if seed is not None:
        cmd_parts.extend(["-s", str(seed)])

    cmd_parts.extend(["--throttle", str(throttle_ms)])

    if verbose:
        cmd_parts.append("-" + "v" * min(verbose, 3))

    if ignore_crashes:
        cmd_parts.append("--ignore-crashes")

    if ignore_timeouts:
        cmd_parts.append("--ignore-timeouts")

    cmd_parts.append(str(event_count))

    console.print(
        f"[bold]Running monkey: {event_count} events"
        + (f" on {package}" if package else "")
        + f" (throttle={throttle_ms}ms)"
    )

    # Estimate runtime: events * throttle + 60s overhead margin
    estimated_seconds = (event_count * throttle_ms) // 1000 + 60
    output = adb.shell(*cmd_parts, timeout=max(estimated_seconds, 120))

    # Parse results
    crashes = output.count("// CRASH:") + output.count("FATAL EXCEPTION")
    anrs = output.count("// NOT RESPONDING:")
    console.print(
        f"[bold]Monkey finished: "
        f"[green]{event_count} events[/green], "
        f"[{'red' if crashes else 'green'}]{crashes} crash(es)[/], "
        f"[{'red' if anrs else 'green'}]{anrs} ANR(s)"
    )

    return output


# ── Pretty printing ──────────────────────────────────────────────────


def print_hierarchy(root: UIElement, max_depth: int = 10) -> None:
    """Print the UI hierarchy as a rich Tree."""
    tree = Tree("[bold]UI Hierarchy")
    _add_to_tree(tree, root, depth=0, max_depth=max_depth)
    console.print(tree)


def _add_to_tree(tree: Tree, el: UIElement, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        return
    label_parts = [f"[bold]{el.class_name.rsplit('.', 1)[-1]}[/bold]"]
    if el.text:
        label_parts.append(f'text="{el.text}"')
    if el.resource_id:
        label_parts.append(f"id={el.resource_id}")
    if el.content_desc:
        label_parts.append(f'desc="{el.content_desc}"')
    if el.clickable:
        label_parts.append("[cyan]clickable[/cyan]")
    l, t, r, b = el.bounds
    label_parts.append(f"[dim][{l},{t}][{r},{b}][/dim]")

    branch = tree.add(" ".join(label_parts))
    for child in el.children:
        _add_to_tree(branch, child, depth + 1, max_depth)


def print_clickable(root: UIElement) -> None:
    """Print a table of all clickable elements."""
    clickable = find_clickable(root)
    if not clickable:
        console.print("[dim]No clickable elements found.")
        return

    t = Table(title=f"Clickable Elements ({len(clickable)})", show_lines=True)
    t.add_column("Text", style="bold")
    t.add_column("Resource ID")
    t.add_column("Class")
    t.add_column("Desc")
    t.add_column("Centre")
    t.add_column("Bounds")

    for el in clickable:
        x, y = el.centre
        l, top, r, b = el.bounds
        t.add_row(
            el.text or "[dim]—",
            el.resource_id or "[dim]—",
            el.class_name.rsplit(".", 1)[-1],
            el.content_desc or "[dim]—",
            f"({x}, {y})",
            f"[{l},{top}][{r},{b}]",
        )
    console.print(t)
