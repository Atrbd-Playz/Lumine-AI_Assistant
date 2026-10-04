"""Where a request should happen, and why. No machine, no browser, no network.

The router is the layer that fixes the reported symptoms -- "she opened Chrome
*and* her own browser", "she opened Krita twice", "she used a search API when I
asked her to look at the page" -- and it is the only layer where all three are
visible at once. So these tests are the acceptance table from the requirements,
plus the negative cases, because a router that only handles the examples will
misroute everything nobody thought of.

`MachineFacts` is built by the caller, which is what makes the whole file
offline: installed apps, running processes, open windows and the browser state
are all inputs.
"""

from __future__ import annotations

import unittest

from agent.runtime import router


def facts(**kwargs) -> router.MachineFacts:
    return router.MachineFacts.build(**kwargs)


CLEAN = facts()
WITH_KRITA = facts(installed={"krita", "krita pencil", "google chrome", "microsoft edge", "notepad"})
WITH_PWA = facts(installed={"instagram (www.instagram.com)", "whatsapp", "krita"})
ALL_TOOLS = ["search_web", "open_app", "browse", "web_control", "send_whatsapp", "run_task"]


class EnvironmentFactsTests(unittest.TestCase):
    def test_own_browser_phrase_beats_everything(self):
        # The strongest rule: the user has already answered the question. Even
        # with a matching program installed, "in your browser" is not a program.
        route = router.classify(
            "Open YouTube in your browser and search for a song", WITH_KRITA, tools=ALL_TOOLS
        )
        self.assertEqual(route.environment, router.BROWSER)
        self.assertEqual(route.url, "https://www.youtube.com")
        self.assertEqual(route.tool, "browse")
        self.assertFalse(route.ambiguous)

    def test_naming_chrome_is_desktop_and_never_our_browser(self):
        route = router.classify("Open Chrome", WITH_KRITA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertEqual(route.tool, "open_app")
        self.assertEqual(route.notes["navigation"], "unavailable")

    def test_a_named_browser_does_not_also_start_the_isolated_one(self):
        # The specific reported bug. Chrome named => desktop route, and the
        # browser tool is not the one chosen.
        route = router.classify("Open Chrome and go to YouTube", WITH_KRITA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertNotEqual(route.tool, "browse")
        self.assertIn("unavailable", route.notes.get("navigation", ""))

    def test_an_installed_program_named_alone_routes_to_the_desktop(self):
        route = router.classify("Lumine, open Krita.", WITH_KRITA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertEqual(route.target, "krita")
        self.assertEqual(route.tool, "open_app")

    def test_a_running_program_is_reported_as_such(self):
        route = router.classify("open krita", facts(installed={"krita"}, running={"krita.exe": 2}), tools=ALL_TOOLS)
        self.assertIn("already running", route.reason)
        self.assertTrue(route.notes["alreadyRunning"])

    def test_a_bare_site_name_uses_the_web_even_with_a_pwa_installed(self):
        """The old Scenario D, inverted on purpose.

        This used to assert the opposite: "Open Instagram" with the PWA installed
        had to start the app rather than a browser window. It now uses the web,
        because a bare name means the website and "desktop" means the program --
        one rule for every site, instead of a table that had to be right about each
        one. Naming the app still works, which is the next test.
        """
        route = router.classify("Open Instagram.", WITH_PWA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)

    def test_naming_the_app_reaches_the_installed_pwa(self):
        route = router.classify("open desktop instagram", WITH_PWA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertEqual(route.tool, "open_app")

    def test_a_site_named_with_the_desktop_claim_stays_on_the_desktop(self):
        route = router.classify("open instagram on my desktop", WITH_PWA, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)


class VerbRoutingTests(unittest.TestCase):
    def test_a_price_question_is_a_lookup_not_a_page(self):
        # Scenario F: no browser window for a fact a search answers.
        route = router.classify("Find the current price range of white T-shirts.", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BACKGROUND)
        self.assertEqual(route.tool, "search_web")

    def test_a_question_about_a_reply_reads_rather_than_sends(self):
        """"Did Afia reply?" must not reach for the tool that sends.

        ``reply`` was listed in ``_WRITING`` because "reply to Afia" is an
        instruction to write -- but "did Afia reply?" is a question about what was
        written, and it carried ``did`` while carrying the word that meant "write".
        The result was the single worst routing outcome available: a question
        routed to the send tool.
        """
        facts = router.MachineFacts.build(installed={"whatsapp"})
        for request in (
            "did afia reply?",
            "has afia replied?",
            "any reply from afia?",
            "did afia respond?",
            "has afia replied to my message?",
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    router.classify(request, facts, tools=ALL_TOOLS).tool,
                    "whatsapp_read",
                )

    def test_an_instruction_to_reply_still_sends(self):
        # The other half, and the reason `reply` could not simply be deleted: the
        # imperative carries no interrogative, so it falls to the send tool.
        facts = router.MachineFacts.build(installed={"whatsapp"})
        for request in ("reply to afia saying hi", "reply back to afia"):
            with self.subTest(request=request):
                self.assertEqual(
                    router.classify(request, facts, tools=ALL_TOOLS).tool,
                    "send_whatsapp",
                )

    def test_an_explicit_write_still_beats_a_question_in_the_same_sentence(self):
        # "did she get my message? no -- send it again" is two requests, and the
        # second is the one being acted on.
        route = router.classify(
            "did she get my message? no, send it again on whatsapp",
            router.MachineFacts.build(installed={"whatsapp"}),
            tools=ALL_TOOLS,
        )
        self.assertEqual(route.tool, "send_whatsapp")

    def test_a_site_name_that_is_also_installed_still_opens_the_website(self):
        """The reported bug: "open youtube" launched the desktop app.

        YouTube was in a table of names that preferred the app, and the
        installed-program step ran *before* the site rule and returned first, so
        the table was unreachable and any installed app of the same name won.
        "play something on youtube" was worse: no one means that as a launch.
        """
        facts = router.MachineFacts.build(installed={"youtube"})
        for request in ("open youtube", "play something on youtube", "search youtube for lofi"):
            with self.subTest(request=request):
                route = router.classify(request, facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.BROWSER)

    def test_the_desktop_name_still_matches_an_installed_app(self):
        # The installed name is whatever the machine called it, so the match cannot
        # be string equality against the site's canonical name.
        facts = router.MachineFacts.build(installed={"YouTube Desktop"})
        self.assertEqual(
            router.classify("open youtube", facts, tools=ALL_TOOLS).environment,
            router.BROWSER,
        )

    def test_saying_the_app_wants_the_app(self):
        # Step 3 steps aside for a site name, so a user who genuinely wants the
        # installed program needs a way to say so. Both spellings.
        facts = router.MachineFacts.build(installed={"youtube"})
        for request in ("open youtube on my desktop", "open the youtube app"):
            with self.subTest(request=request):
                self.assertEqual(
                    router.classify(request, facts, tools=ALL_TOOLS).environment,
                    router.DESKTOP,
                )

    def test_the_web_is_the_default_for_every_named_site(self):
        """The rule, stated once: say "desktop" for the program, otherwise the web.

        This replaced two tables -- names that preferred the app, and names that
        preferred the web. A table has to be right about every site, and each entry
        was another chance to launch a program nobody asked for.
        """
        for site in ("youtube", "instagram", "netflix", "spotify", "maps"):
            with self.subTest(site=site):
                facts = router.MachineFacts.build(installed={site})
                route = router.classify(f"open {site}", facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.BROWSER, f"open {site} should use the web")

    def test_a_program_that_is_not_a_website_still_opens_the_program(self):
        """The rule is "the web *when there is one*", not "always the web".

        Discord and Telegram have no entry in `SITES`, so there is no page to
        open and the installed program is the only answer available. Forcing a
        browser here would mean inventing a URL, and a wrong guess is worse than
        the honest program launch.
        """
        for name in ("discord", "telegram"):
            with self.subTest(name=name):
                self.assertNotIn(name, router.SITES)
                facts = router.MachineFacts.build(installed={name})
                self.assertEqual(
                    router.classify(f"open {name}", facts, tools=ALL_TOOLS).environment,
                    router.DESKTOP,
                )

    def test_naming_the_desktop_app_opens_the_program_for_any_site(self):
        for site in ("youtube", "instagram", "netflix", "spotify"):
            with self.subTest(site=site):
                facts = router.MachineFacts.build(installed={site})
                self.assertEqual(
                    router.classify(f"open desktop {site}", facts, tools=ALL_TOOLS).environment,
                    router.DESKTOP,
                )

    def test_the_products_own_word_order_counts_as_naming_the_app(self):
        # "WhatsApp Desktop" is the product's real name and speech-to-text writes it
        # that way. Without this, "open whatsapp desktop" was a messaging request.
        facts = router.MachineFacts.build(installed={"whatsapp"})
        for request in ("open whatsapp desktop", "whatsapp desktop"):
            with self.subTest(request=request):
                route = router.classify(request, facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.DESKTOP)
                self.assertEqual(route.tool, "open_app")

    def test_my_desktop_does_not_by_itself_name_an_application(self):
        # The reverse-order pattern must not swallow "on my desktop", which names
        # the machine rather than a program.
        facts = router.MachineFacts.build(installed={"whatsapp"})
        self.assertEqual(
            router.classify("message her on my desktop", facts, tools=ALL_TOOLS).environment,
            router.MESSAGING,
        )

    def test_asking_for_an_app_that_is_not_installed_says_so(self):
        # Saying "desktop" and silently getting the web would look like it worked,
        # which is the failure this rule exists to remove.
        route = router.classify(
            "open desktop youtube",
            router.MachineFacts.build(installed=set()),
            tools=ALL_TOOLS,
        )
        self.assertTrue(route.ambiguous)
        self.assertIn("desktop app", route.question)

    def test_a_named_browser_is_still_a_program_launch(self):
        # Step 2 runs before all of this and is unchanged: naming Chrome names a
        # program, and the route promises only the launch and the focus.
        facts = router.MachineFacts.build(installed={"brave"})
        self.assertEqual(
            router.classify("open brave", facts, tools=ALL_TOOLS).environment,
            router.DESKTOP,
        )

    def test_the_policy_states_the_web_is_the_default_for_a_site(self):
        # The router can be right and the model still reach for `open_app`, because
        # it is choosing the tool and not consulting the router. The rule has to be
        # written where the model reads it.
        from agent.tools.tools_policy import TOOL_USAGE_POLICY

        self.assertIn("the website is the default", TOOL_USAGE_POLICY)
        self.assertIn("desktop", TOOL_USAGE_POLICY)

    def test_a_drawing_tablet_price_is_a_lookup_too(self):
        route = router.classify("What's the price of a drawing tablet?", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BACKGROUND)

    def test_searching_on_a_named_site_is_the_site(self):
        # The same verb as the lookup above, a different destination -- which is
        # why this cannot be a verb table.
        route = router.classify("Search for a song on YouTube", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertEqual(route.target, "youtube")

    def test_a_product_page_request_is_the_browser(self):
        route = router.classify(
            "Open this product page and select its size.", CLEAN, tools=ALL_TOOLS
        )
        self.assertIn(route.environment, (router.BROWSER, router.BACKGROUND))
        # "select" is an interaction verb, so it must not be a plain lookup.
        self.assertNotEqual(route.tool, "search_web")

    def test_an_interaction_with_no_page_open_asks_which_site(self):
        route = router.classify("scroll down", CLEAN, tools=ALL_TOOLS)
        self.assertTrue(route.ambiguous)
        self.assertIn("website", route.question.casefold())

    def test_a_positional_request_with_a_page_open_uses_that_page(self):
        # "Open the third result" is not an `open` of anything: the only thing it
        # can mean is the page in front of the user. Falling through to a
        # background search here is what made "she cannot click anything".
        route = router.classify("open the third result", facts(browser_open=True), tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertFalse(route.ambiguous)

    def test_a_positional_request_with_no_page_asks_which_page(self):
        route = router.classify("open the third result", CLEAN, tools=ALL_TOOLS)
        self.assertTrue(route.ambiguous)
        self.assertIn("page", route.question.casefold())

    def test_a_named_site_with_a_search_verb_is_that_site_not_a_web_search(self):
        # The reported "she cannot search on YouTube". A search box only exists
        # there, so a background lookup cannot answer it at all.
        route = router.classify("Open YouTube and search for lofi", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertEqual(route.url, "https://www.youtube.com")

    def test_a_search_on_a_named_site_is_the_site(self):
        route = router.classify("Search YouTube for a nursery rhyme", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)

    def test_a_price_mentioning_no_site_is_still_a_lookup(self):
        # The other direction, and the one a site rule could break: naming no site
        # must not turn a fact into a page.
        route = router.classify("Find the price of a drawing tablet", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BACKGROUND)

    def test_an_interaction_with_a_page_already_open_uses_it(self):
        route = router.classify("click the second video", facts(browser_open=True), tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertFalse(route.ambiguous)

    def test_an_address_in_the_request_is_the_browser(self):
        route = router.classify("open example.com/pricing", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertIn("example.com", route.url)

    def test_a_bare_domain_is_the_browser(self):
        route = router.classify("go to github.com", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BROWSER)

    def test_a_window_request_is_the_desktop(self):
        route = router.classify("close the Krita window", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertEqual(route.tool, "manage_window")

    def test_messaging_routes_to_messaging_and_is_sensitive(self):
        route = router.classify("Send Afia a message saying I'll be there in 20 minutes.", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.MESSAGING)
        self.assertEqual(route.tool, "send_whatsapp")
        self.assertTrue(route.sensitive)

    def test_nothing_actionable_stays_a_conversation(self):
        route = router.classify("I had a lovely day today", CLEAN, tools=ALL_TOOLS)
        self.assertEqual(route.environment, router.BACKGROUND)
        self.assertIn("no tool call", route.reason)


    def test_a_browser_can_still_be_asked_for_explicitly(self):
        facts = router.MachineFacts.build(installed={"instagram"})
        self.assertEqual(
            router.classify("open instagram in your browser", facts, tools=ALL_TOOLS).environment,
            router.BROWSER,
        )

    def test_a_program_that_is_not_a_website_is_unaffected(self):
        facts = router.MachineFacts.build(installed={"notepad"})
        self.assertEqual(
            router.classify("open notepad", facts, tools=ALL_TOOLS).environment,
            router.DESKTOP,
        )


class WhatsAppMeansWebTests(unittest.TestCase):
    """Whether a bare "whatsapp" reaches Web, and naming the app reaches the app.

    The reported bug was two windows for one sentence: "open whatsapp" launched
    WhatsApp Web *and* the installed WhatsApp Desktop. Neither rule was wrong on
    its own -- the site rule preferred an installed app, and the messaging rule
    claimed anything naming "whatsapp" -- so each was correct and the pair produced
    the wrong answer.

    WhatsApp is no longer a special case. The general rule is that the web is the
    default for every named site and the application is reached by naming it, so
    these tests are the WhatsApp end of that rule rather than a policy of their own.
    What is specific here is *which* rule reaches it, because the messaging rule
    runs first and used to swallow "desktop whatsapp" whole.
    """

    def setUp(self):
        # A machine with WhatsApp Desktop installed *and already running*, which is
        # exactly the state that produced two windows.
        self.facts = router.MachineFacts.build(
            installed={"whatsapp"},
            running={"whatsapp.exe": 1},
            windows={"whatsapp"},
        )

    def test_a_bare_whatsapp_never_reaches_the_desktop_program(self):
        for request in (
            "open whatsapp",
            "whatsapp",
            "send a message on whatsapp",
            "check my whatsapp",
        ):
            with self.subTest(request=request):
                route = router.classify(request, self.facts, tools=ALL_TOOLS)
                self.assertNotEqual(
                    route.tool,
                    "open_app",
                    "a bare 'whatsapp' launched the installed app",
                )

    def test_the_messaging_tools_are_where_a_bare_whatsapp_lands(self):
        # Not merely "not the app" -- it should reach the thing that can actually do
        # the work, which is the Web-backed messaging tools.
        for request in ("send a message on whatsapp", "whatsapp"):
            with self.subTest(request=request):
                route = router.classify(request, self.facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.MESSAGING)

    def test_naming_the_desktop_app_reaches_the_desktop_program(self):
        for request in ("open desktop whatsapp", "open the whatsapp app"):
            with self.subTest(request=request):
                route = router.classify(request, self.facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.DESKTOP)
                self.assertEqual(route.tool, "open_app")

    def test_without_the_app_installed_the_word_falls_back_to_web(self):
        """"Desktop WhatsApp" on a machine that has no WhatsApp Desktop.

        It lands on Web rather than refusing, and that is deliberate. The user asked
        for *WhatsApp* and added a qualifier about where; the qualifier cannot be
        honoured because the program is not there, but the thing they wanted is
        openable. Refusing would be technically honest and practically wrong -- it
        turns "open WhatsApp on my desktop" into a dead end on a machine where the
        desktop app was simply never installed.

        Asserted as a fact about the fallback rather than as a preference, so that
        changing it is a deliberate act with its reason written down.
        """
        facts = router.MachineFacts.build(installed={"chrome"})
        route = router.classify("open desktop whatsapp", facts, tools=ALL_TOOLS)
        self.assertEqual(route.tool, "browse")
        self.assertEqual(route.target, "whatsapp")

    def test_on_my_desktop_alone_is_not_a_request_for_the_app(self):
        """"message her on my desktop" is about the machine, not the program.

        The phrase the fix could easily have broken in the other direction:
        wants_desktop is true for it, so if that alone suppressed the messaging
        rule, the most ordinary sentence in the product would have launched an app
        instead of sending a message.
        """
        for request in (
            "message her on my desktop",
            "send a message from my desktop",
        ):
            with self.subTest(request=request):
                route = router.classify(request, self.facts, tools=ALL_TOOLS)
                self.assertEqual(route.environment, router.MESSAGING)

    def test_reading_is_still_a_read(self):
        route = router.classify("did she reply on whatsapp", self.facts, tools=ALL_TOOLS)
        self.assertEqual(route.tool, "whatsapp_read")

    def test_there_is_no_table_left_to_disagree(self):
        # The two tables are gone, so this test is the guard against them coming
        # back. A name-based default means a table has to be right about every
        # site, and each entry was another chance to launch a program nobody asked
        # for. The rule is now one sentence and applies to every name.
        for gone in ("DESKTOP_SITE_APPS", "WEB_FIRST_SITE_APPS"):
            with self.subTest(name=gone):
                self.assertFalse(
                    hasattr(router, gone),
                    f"{gone} is back; the web is the default for every named site",
                )



class DisabledRouteTests(unittest.TestCase):
    def test_a_disabled_browser_falls_back_to_a_lookup_and_asks(self):
        route = router.classify("Open YouTube in your browser", CLEAN, tools=["search_web", "open_app"])
        self.assertTrue(route.ambiguous)
        self.assertIn("Chrome", route.question)

    def test_a_disabled_launch_tool_refuses_the_desktop_route(self):
        # With `open_app` off, "open Krita" must not plan a launch. The route
        # reports that it cannot run, and the caller asks rather than starting a
        # program the user switched off.
        route = router.classify("open Krita", WITH_KRITA, tools=["search_web", "browse"])
        self.assertNotEqual(route.tool, "open_app")

    def test_a_disabled_messaging_tool_asks_rather_than_reaching_around(self):
        route = router.classify("message Afia", CLEAN, tools=["search_web", "open_app"])
        self.assertTrue(route.ambiguous)
        self.assertIn("switched off", route.question)

    def test_nothing_enabled_is_treated_as_everything_available(self):
        # An empty list means "the caller did not say", which is not the same as
        # "everything is off". Treating it as off would break every standalone
        # caller, including the tests for the other rules.
        route = router.classify("open Krita", WITH_KRITA, tools=None)
        self.assertEqual(route.tool, "open_app")


class ExplainabilityTests(unittest.TestCase):
    def test_every_route_records_why_and_what_the_alternatives_were(self):
        # "She opened a browser when I asked for the app" is only diagnosable if
        # the alternatives were recorded at the time.
        for request in (
            "Open Krita.",
            "Find the current price of white T-shirts.",
            "Open YouTube in your browser.",
            "Open Chrome.",
        ):
            with self.subTest(request=request):
                route = router.classify(request, WITH_KRITA, tools=ALL_TOOLS)
                self.assertTrue(route.reason)
                self.assertIsInstance(route.alternatives, tuple)

    def test_a_route_serializes_without_the_empty_fields(self):
        payload = router.classify("open Krita", WITH_KRITA, tools=ALL_TOOLS).to_dict()
        self.assertEqual(payload["environment"], router.DESKTOP)
        self.assertNotIn("question", payload)
        self.assertNotIn("ambiguous", payload)

    def test_an_ambiguous_route_says_so_in_its_payload(self):
        payload = router.classify("click the second video", CLEAN, tools=ALL_TOOLS).to_dict()
        self.assertTrue(payload["ambiguous"])
        self.assertTrue(payload["question"])


class ConfiguredDefaultsTests(unittest.TestCase):
    """A preference the user wrote down beats a guess; no preference means ask."""

    def test_an_unresolved_open_uses_the_configured_own_browser(self):
        route = router.classify(
            "open a website",
            facts(own_browser_default=True),
            tools=ALL_TOOLS,
        )
        self.assertEqual(route.environment, router.BROWSER)
        self.assertEqual(route.notes["preferred"], "own_browser")

    def test_an_unresolved_open_uses_the_configured_desktop_browser(self):
        route = router.classify(
            "open a website", facts(default_browser="chrome"), tools=ALL_TOOLS
        )
        self.assertEqual(route.environment, router.DESKTOP)
        self.assertEqual(route.target, "chrome")
        # The honest limitation travels with it: this product cannot navigate the
        # user's own browser.
        self.assertEqual(route.notes["navigation"], "unavailable")

    def test_with_no_preference_the_router_asks(self):
        route = router.classify("open a website", CLEAN, tools=ALL_TOOLS)
        self.assertTrue(route.ambiguous)
        self.assertTrue(route.question)

    def test_a_preference_never_overrides_a_named_browser(self):
        # Explicit intent beats every default, including a configured one.
        route = router.classify(
            "Open YouTube in your browser",
            facts(default_browser="chrome", own_browser_default=True),
            tools=ALL_TOOLS,
        )
        self.assertEqual(route.environment, router.BROWSER)
        self.assertNotIn("preferred", route.notes)

    def test_a_preference_that_names_a_disabled_tool_falls_through_to_the_question(self):
        route = router.classify(
            "open a website", facts(default_browser="chrome"), tools=["search_web"]
        )
        self.assertTrue(route.ambiguous)


class SiteTableTests(unittest.TestCase):
    def test_site_names_match_whole_tokens_only(self):
        # "Instagram" must match; a sentence containing "ig" must not open an
        # account. Whole-token matching is what makes this safe.
        self.assertEqual(router.find_site("look at instagram")[0], "instagram")
        self.assertIsNone(router.find_site("check sig once more"))

    def test_an_unknown_site_is_not_guessed_at(self):
        self.assertIsNone(router.find_site("check example forum thread"))

    def test_every_site_has_a_https_address_and_a_host(self):
        for name, meta in router.SITES.items():
            with self.subTest(site=name):
                self.assertTrue(meta["url"].startswith("https://"))
                self.assertTrue(meta["host"])

    def test_the_verb_families_do_not_overlap_into_confusion(self):
        # "search" is a lookup verb and "click" is an interaction verb; a
        # request containing both is doing both, and the router must be able to
        # see that rather than collapsing them.
        families = router.verb_families("click the search results")
        self.assertIn("interact", families)
        self.assertIn("lookup", families)


if __name__ == "__main__":
    unittest.main()
