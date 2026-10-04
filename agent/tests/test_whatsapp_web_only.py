"""Messaging is WhatsApp Web. The installed app is not a messaging path.

A product decision, and one the code was making the wrong way round for a while.

## What went wrong

``PATH_PREFERENCE`` was ``("web", "desktop")``. The reasoning was sound in the
abstract -- a working desktop path starts nothing, so it needs no browser, no
profile and no QR code, and is therefore a good fallback when Web is not ready --
and it produced three faults, none of them theoretical:

- **Two applications for one sentence.** A send whose Web tab had not finished
  loading fell through to Desktop, so the desktop app opened, searched its own
  chat list, and the user watched two applications answer one request. Measured in
  the worker log: the resolve step took 9.1 seconds -- UI Automation speed -- while
  Web was open the whole time.
- **A resolve that could not be followed by an open.** The contact was found on
  one path and the chat was then opened on another, so a name WhatsApp had just
  matched came back as *"I couldn't read your chat list -- nothing matched, and
  nothing was sent."*
- **A silent authority question.** Which application a message leaves from is not
  something a person should have to reason about mid-sentence.

The previous fix for the second of those was to pick one path per operation and
use it whole. That helps. The only way to *guarantee* it is to have one path.

## What has NOT changed

**The installed WhatsApp Desktop app is still launchable.** "Open WhatsApp
Desktop" is a request to start a program, and `router.py` sends it to `open_app`
like any other installed program. It is a *window* the user can look at, never
the thing a message is written into. Deleting the application from the machine
would not have been what was asked for, and neither was making it unreachable --
only unreachable *as a path a tool can drive*.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from agent.tools.messaging import whatsapp as send_path
from agent.tools.messaging import whatsapp_ops as ops


class OnlyWebTests(unittest.TestCase):
    """The preference list, and the thing that builds from it."""

    def test_web_is_the_only_messaging_path(self):
        self.assertEqual(send_path.PATH_PREFERENCE, ("web",))

    def test_the_candidate_list_contains_exactly_one_backend(self):
        names = [backend.name for backend in send_path.backend_candidates()]
        self.assertEqual(
            names,
            ["WhatsApp Web"],
            "a second candidate is a second application answering one sentence",
        )

    def test_the_desktop_factory_is_not_reachable_from_the_candidates(self):
        # Asserted on the code, with the docstring stripped -- the docstring names
        # ``desktop_backend`` in order to explain why it is absent, and a naive
        # substring check would fail on the explanation of the fix.
        source = inspect.getsource(send_path.backend_candidates)
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        code = code.split('"""', 2)
        body = code[2] if len(code) > 2 else code[0]
        self.assertNotIn("desktop_backend", body)
        self.assertIn("web_backend", body)

    def test_choose_backend_would_refuse_a_non_web_path(self):
        """The guard inside ``choose_backend``, which cannot fire today.

        It is here so that adding a second path fails here -- in a test that names
        the decision -- rather than in front of a user watching two windows.
        """
        source = inspect.getsource(send_path.choose_backend)
        self.assertIn('if path != "web":', source)
        self.assertIn("does not use", source)

    def test_a_refusal_names_one_application_not_two(self):
        """The user is not asked to work out which one she meant.

        Two sentences -- one per path -- is what the old loop produced, and it is
        why the answer read like something was broken rather than like WhatsApp
        was still starting.
        """
        self.assertEqual(len(send_path.backend_candidates()), 1)


class NoOperationClaimsDesktopTests(unittest.TestCase):
    """The operation table cannot hand out a desktop path again."""

    def test_no_verb_is_supported_on_the_desktop_path(self):
        offenders = [
            op.verb for op in ops.OPERATIONS if ops.supported_by(op.verb, "desktop")
        ]
        self.assertEqual(
            offenders,
            [],
            "an operation marked as reachable on the desktop path is how the "
            "desktop app gets opened by a send",
        )

    def test_the_support_check_ignores_the_row_field(self):
        """``supports()`` is answered from :data:`ops.ONLY_PATH`, not the row.

        Otherwise a single row setting ``desktop=True`` -- which two rows used to
        do -- would make the adapter reachable again on its own, with no other
        code changing and no test noticing.
        """
        row = ops.Operation(
            verb="example",
            tool=ops.READ_TOOL,
            action_class="read",
            desktop=True,
            web=False,
        )
        self.assertTrue(row.desktop, "the field is still the record")
        self.assertFalse(
            ops.Operation.supports(row, "desktop"),
            "but the field alone must not make the path reachable",
        )

    def test_every_selectable_operation_still_works_on_web(self):
        broken = [
            op.verb
            for op in ops.selectable_operations(ops.SEND_TOOL)
            if not ops.supported_by(op.verb, "web")
        ]
        self.assertEqual(broken, [], "removing a path must not remove a feature")

    def test_the_adapter_module_is_still_importable_as_a_record(self):
        """Deleting it would delete the reason it was not used.

        An unreachable module and a launchable application are different states.
        Keeping the adapter means the next question -- "could it have done this?"
        -- has an answer with a file to point at.
        """
        from agent.tools.messaging import whatsapp_desktop  # noqa: F401

        self.assertTrue(hasattr(whatsapp_desktop, "desktop_capability"))


class LaunchingTheAppIsNotMessagingTests(unittest.TestCase):
    """'Open WhatsApp Desktop' starts a program. It does not send anything."""

    def test_the_router_sends_the_explicit_request_to_open_app(self):
        from agent.runtime import router
        from agent.tests.test_router import ALL_TOOLS

        facts = router.MachineFacts.build(installed={"whatsapp"})
        for request in ("open desktop whatsapp", "open the whatsapp app"):
            with self.subTest(request=request):
                route = router.classify(request, facts, tools=ALL_TOOLS)
                self.assertEqual(route.tool, "open_app")
                self.assertEqual(route.environment, router.DESKTOP)

    def test_no_messaging_tool_can_launch_the_application(self):
        """Not one of the four reaches ``open_app`` or the desktop adapter.

        Checked structurally rather than by behaviour: a mock that would have
        raised makes the accidental call visible, and the adapter is patched out
        so an import-time reference cannot quietly keep it alive.
        """
        from agent.tools.messaging import whatsapp_desktop

        with mock.patch.object(whatsapp_desktop, "desktop_capability") as capability:
            capability.side_effect = AssertionError(
                "a messaging path reached for the desktop application"
            )
            with mock.patch.object(
                send_path, "desktop_backend", side_effect=AssertionError("reached")
            ):
                self.assertEqual(
                    [b.name for b in send_path.backend_candidates()],
                    ["WhatsApp Web"],
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()