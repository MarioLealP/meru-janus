import pytest

from janus.remove import Unremovable, is_removed, mark_removed

KITSUNE = """# a comment stays
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: kitsune
  approvedBy: akadmin
spec:
  owner: mario
  description: test  # so does this
  route:
    enabled: false
"""


def test_removed_goes_last_in_spec_and_nothing_else_moves():
    marked = mark_removed(KITSUNE)
    assert marked == KITSUNE + "  removed: true\n"
    assert is_removed(marked) and not is_removed(KITSUNE)


def test_spec_in_the_middle_and_blank_lines_inside():
    text = "metadata:\n  name: x\nspec:\n  owner: mario\n\n  route:\n    enabled: false\n\nstatus: {}\n"
    assert mark_removed(text) == (
        "metadata:\n  name: x\nspec:\n  owner: mario\n\n  route:\n    enabled: false\n  removed: true\n\nstatus: {}\n"
    )


def test_an_existing_removed_false_is_flipped_not_doubled():
    text = "spec:\n  owner: mario\n  removed: false\n"
    assert mark_removed(text) == "spec:\n  owner: mario\n  removed: true\n"


def test_a_nested_removed_is_not_the_one():
    text = "spec:\n  route:\n    removed: false\n"
    assert mark_removed(text) == "spec:\n  route:\n    removed: false\n  removed: true\n"
    assert not is_removed("spec:\n  route:\n    removed: false\n")


@pytest.mark.parametrize("text", ["kind: App\n", "spec: {owner: mario}\n", "spec:\nkind: App\n"])
def test_layouts_it_cant_edit_safely_are_refused(text: str):
    with pytest.raises(Unremovable):
        mark_removed(text)
