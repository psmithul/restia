"""Behavioral contracts for Restia's DOM-free V2 navigation registry."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "static" / "js" / "navigation-registry.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(body: str):
    source = f"""
      import * as nav from {json.dumps(REGISTRY.as_uri())};
      {body}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_registry_is_valid_immutable_and_covers_current_destinations():
    result = _node_eval(
        """
        const required = [
          'home', 'chat', 'new-chat', 'search', 'delete-session', 'toggle-sidebar',
          'inbox', 'life', 'projects', 'tasks', 'calendar', 'todos', 'library', 'documents',
          'notes', 'memory', 'study', 'research', 'messages', 'email',
          'compare', 'cookbook', 'gallery', 'theme', 'settings', 'activity', 'notifications', 'profile',
          'quick-note', 'quick-todo', 'new-message', 'share-moment',
          'compose-email', 'new-document', 'manage-chats', 'new-model-chat',
        ];
        let mutationBlocked = false;
        try { nav.NAVIGATION_ITEMS.push({}); } catch (_) { mutationBlocked = true; }
        console.log(JSON.stringify({
          errors: nav.validateNavigationRegistry(),
          ids: nav.NAVIGATION_ITEMS.map(item => item.id),
          sameAlias: nav.NAVIGATION_REGISTRY === nav.NAVIGATION_ITEMS,
          frozen: Object.isFrozen(nav.NAVIGATION_ITEMS)
            && nav.NAVIGATION_ITEMS.every(item => Object.isFrozen(item)
              && Object.isFrozen(item.legacyIds)
              && Object.isFrozen(item.capabilities)),
          mutationBlocked,
          required,
        }));
        """
    )

    assert result["errors"] == []
    assert set(result["ids"]) == set(result["required"])
    assert result["sameAlias"] is True
    assert result["frozen"] is True
    assert result["mutationBlocked"] is True


def test_v3_primary_hierarchy_is_exact_and_specialists_do_not_own_mobile_tabs():
    result = _node_eval(
        """
        const mobile = nav.NAVIGATION_ITEMS
          .filter(item => item.surfaces.includes('mobile') && item.id !== 'toggle-sidebar')
          .map(item => item.id);
        console.log(JSON.stringify({ primary: nav.PRIMARY_NAVIGATION_IDS, mobile }));
        """
    )

    assert result["primary"] == ["chat", "home", "inbox", "life", "search"]
    assert set(result["mobile"]) == set(result["primary"])


def test_every_current_icon_rail_control_has_one_stable_owner():
    result = _node_eval(
        """
        const railIds = [
          'rail-restia', 'rail-home', 'rail-life', 'rail-activity',
          'rail-search-btn', 'rail-new-session', 'rail-delete-session',
          'rail-chats', 'rail-documents', 'rail-messages', 'rail-calendar',
          'rail-compare', 'rail-cookbook', 'rail-research', 'rail-email',
          'rail-gallery', 'rail-archive', 'rail-memory', 'rail-notes',
          'rail-inbox', 'rail-projects', 'rail-study', 'rail-todos', 'rail-tasks',
          'rail-theme', 'rail-settings',
        ];
        console.log(JSON.stringify(Object.fromEntries(railIds.map(id => [
          id, nav.findNavigationItemByLegacyId('#' + id)?.id || null,
        ]))));
        """
    )

    assert result == {
        "rail-restia": "chat",
        "rail-home": "home",
        "rail-life": "life",
        "rail-activity": "activity",
        "rail-search-btn": "search",
        "rail-new-session": "new-chat",
        "rail-delete-session": "delete-session",
        "rail-chats": "chat",
        "rail-documents": "documents",
        "rail-messages": "messages",
        "rail-calendar": "calendar",
        "rail-compare": "compare",
        "rail-cookbook": "cookbook",
        "rail-research": "research",
        "rail-email": "email",
        "rail-gallery": "gallery",
        "rail-archive": "library",
        "rail-memory": "memory",
        "rail-notes": "notes",
        "rail-inbox": "inbox",
        "rail-projects": "projects",
        "rail-study": "study",
        "rail-todos": "todos",
        "rail-tasks": "tasks",
        "rail-theme": "theme",
        "rail-settings": "settings",
    }


def test_command_palette_metadata_preserves_existing_public_ids():
    result = _node_eval(
        """
        const specs = nav.NAVIGATION_ITEMS
          .filter(item => item.command)
          .map(item => ({ owner: item.id, ...item.command }));
        console.log(JSON.stringify(specs));
        """
    )

    by_id = {entry["id"]: entry for entry in result}
    assert set(by_id) == {
        "inbox", "life", "projects", "study", "messages", "notes", "tasks", "todos", "calendar", "documents",
        "gallery", "research", "compare", "cookbook", "memory", "email",
        "settings", "notifications", "search", "quick-note", "quick-todo", "new-chat",
        "new-message", "share-moment", "theme", "home", "activity",
    }
    # The old palette calls the Library command "documents". Keeping that id
    # is required so its persisted recent-command history remains valid.
    assert by_id["documents"]["owner"] == "library"
    assert by_id["documents"]["triggerIds"] == ["tool-library-btn", "rail-documents"]
    assert by_id["quick-note"]["handler"] == "quick-capture:note"
    assert by_id["new-message"]["afterTriggerId"] == "msg-newchat-btn"


def test_lookup_helpers_resolve_aliases_routes_modals_and_trigger_order():
    result = _node_eval(
        """
        console.log(JSON.stringify({
          brain: nav.getNavigationItem('brain')?.id,
          today: nav.getNavigationItem('today')?.id,
          restia: nav.getNavigationItem('restia')?.id,
          life: nav.getNavigationItem('life-os')?.id,
          maintainer: nav.getNavigationItem('maintainer-center')?.id,
          route: nav.findNavigationItemByRoute('https://restia.local/tasks/?tab=runs#task-1')?.id,
          homeRoute: nav.findNavigationItemByRoute('/today/')?.id,
          activityRoute: nav.findNavigationItemByRoute('/activity')?.id,
          lifeRoute: nav.findNavigationItemByRoute('/life/')?.id,
          root: nav.findNavigationItemByRoute('/')?.id,
          unknownRoute: nav.findNavigationItemByRoute('/missing')?.id || null,
          registeredModal: nav.findNavigationItemByModalId('#calendar-modal')?.id,
          fallbackModal: nav.findNavigationItemByModalId('library-modal')?.id,
          triggers: nav.getLegacyTriggerIds('email'),
          newChatTriggers: nav.getLegacyTriggerIds('new-chat'),
        }));
        """
    )

    assert result == {
        "brain": "memory",
        "today": "home",
        "restia": "chat",
        "life": "life",
        "maintainer": "activity",
        "route": "tasks",
        "homeRoute": "home",
        "activityRoute": "activity",
        "lifeRoute": "life",
        "root": "chat",
        "unknownRoute": None,
        "registeredModal": "calendar",
        "fallbackModal": "library",
        "triggers": ["email-section-title", "rail-email"],
        "newChatTriggers": ["sidebar-new-chat-btn", "rail-new-session"],
    }


@pytest.mark.parametrize(
    ("deep_link", "owner", "params"),
    [
        ("#document-doc%201", "documents", {"id": "doc 1"}),
        ("#open=notes&note=42", "notes", {"id": "42"}),
        ("#email=Sent%2FArchive:991", "email", {"folder": "Sent/Archive", "uid": "991"}),
        ("https://restia.local/#task-task-7", "tasks", {"id": "task-7"}),
        ("#research-run-2", "research", {"id": "run-2"}),
    ],
)
def test_deep_link_matcher_returns_owner_and_decoded_params(deep_link, owner, params):
    result = _node_eval(
        f"""
        const match = nav.matchNavigationDeepLink({json.dumps(deep_link)});
        console.log(JSON.stringify(match && {{ owner: match.item.id, params: match.params }}));
        """
    )
    assert result == {"owner": owner, "params": params}


def test_capability_context_and_preferences_are_evaluated_independently():
    result = _node_eval(
        """
        const cases = {
          defaultResearch: nav.getNavigationAvailability('research'),
          featureOff: nav.getNavigationAvailability('research', {
            features: { deep_research: false }, privileges: { can_use_research: true },
          }),
          privilegeOff: nav.getNavigationAvailability('research', {
            features: { deep_research: true }, privileges: { can_use_research: false },
          }),
          preferenceOff: nav.getNavigationAvailability('research', {
            surface: 'sidebar', features: { deep_research: true },
            privileges: { can_use_research: true }, preferences: { 'tool-research': false },
          }),
          docsDormant: nav.getNavigationAvailability('documents', {
            surface: 'rail', features: { document_editor: true },
            privileges: { can_use_documents: true },
          }),
          docsActive: nav.getNavigationAvailability('documents', {
            surface: 'rail', features: { document_editor: true },
            privileges: { can_use_documents: true },
            contexts: new Set(['document-active-or-present']),
          }),
          docsWrongSurface: nav.getNavigationAvailability('documents', {
            surface: 'sidebar', includeContextual: true,
            features: { document_editor: true }, privileges: { can_use_documents: true },
          }),
          strictUnknown: nav.getNavigationAvailability('memory', { strict: true }),
        };
        console.log(JSON.stringify(cases));
        """
    )

    assert result["defaultResearch"]["available"] is True
    assert result["featureOff"]["available"] is False
    assert "feature:deep_research" in result["featureOff"]["reasons"]
    assert result["privilegeOff"]["available"] is False
    assert "privilege:can_use_research" in result["privilegeOff"]["reasons"]
    assert result["preferenceOff"]["available"] is True
    assert result["preferenceOff"]["visible"] is False
    assert result["docsDormant"]["visible"] is False
    assert result["docsActive"]["visible"] is True
    assert result["docsWrongSurface"]["visible"] is False
    assert result["strictUnknown"]["available"] is False


def test_list_helper_keeps_group_order_and_exposes_hidden_commands_by_surface():
    result = _node_eval(
        """
        console.log(JSON.stringify({
          defaultIds: nav.getNavigationItems().map(item => item.id),
          work: nav.getNavigationItems({ group: 'work' }).map(item => item.id),
          quick: nav.getNavigationItems({ group: 'quick-actions' }).map(item => item.id),
          commands: nav.getNavigationItems({ surface: 'command-palette' }).map(item => item.command?.id),
          sidebarWithResearchHidden: nav.getNavigationItems({
            surface: 'sidebar',
            context: {
              features: { deep_research: true }, privileges: { can_use_research: true },
              preferences: { 'tool-research': false },
            },
          }).map(item => item.id),
        }));
        """
    )

    assert "quick-note" not in result["defaultIds"]
    assert result["work"] == ["inbox", "life", "projects", "tasks", "calendar", "todos"]
    assert result["quick"] == [
        "quick-note", "quick-todo", "new-message", "share-moment",
        "compose-email", "new-document", "manage-chats", "new-model-chat",
    ]
    assert "research" not in result["sidebarWithResearchHidden"]
    assert set(result["commands"]) == {
        "inbox", "life", "projects", "study", "messages", "notes", "tasks", "todos", "calendar", "documents",
        "gallery", "research", "compare", "cookbook", "memory", "email",
        "settings", "notifications", "search", "quick-note", "quick-todo", "new-chat",
        "new-message", "share-moment", "theme", "home", "activity",
    }


def test_v2_home_and_activity_contracts_are_ready_for_shell_integration():
    result = _node_eval(
        """
        const select = id => {
          const item = nav.getNavigationItem(id);
          return {
            id: item.id,
            aliases: item.aliases,
            group: item.group,
            route: item.route,
            surfaces: item.surfaces,
            legacyIds: item.legacyIds,
            containerId: item.containerId,
            command: item.command,
          };
        };
        console.log(JSON.stringify({ home: select('home'), activity: select('activity') }));
        """
    )

    assert result["home"] == {
        "id": "home",
        "aliases": ["today", "mission-control"],
        "group": "home",
        "route": "/today",
        "surfaces": ["rail", "sidebar", "mobile", "command-palette", "route"],
        "legacyIds": {"rail": ["rail-home"], "sidebar": ["v2-home-nav"], "auxiliary": []},
        "containerId": "mission-control-workspace",
        "command": {
            "id": "home",
            "title": "Today",
            "hint": "Open Mission Control",
            "icon": "🏠",
            "keywords": ["home", "today", "mission", "control", "overview"],
            "triggerIds": ["v2-home-nav", "rail-home"],
            "afterTriggerId": None,
            "handler": None,
        },
    }
    assert result["activity"] == {
        "id": "activity",
        "aliases": ["maintainer-center"],
        "group": "system",
        "route": "/activity",
        "surfaces": ["rail", "sidebar", "command-palette", "route"],
        "legacyIds": {"rail": ["rail-activity"], "sidebar": ["v2-activity-nav"], "auxiliary": []},
        "containerId": None,
        "command": {
            "id": "activity",
            "title": "Activity",
            "hint": "Open Activity Center",
            "icon": "📊",
            "keywords": ["activity", "maintainer", "health", "status", "jobs", "runs"],
            "triggerIds": ["v2-activity-nav", "rail-activity"],
            "afterTriggerId": None,
            "handler": None,
        },
    }


def test_validator_reports_duplicate_registry_contracts_and_assertion_throws():
    result = _node_eval(
        """
        const proposal = structuredClone(nav.NAVIGATION_ITEMS);
        proposal.push({ ...structuredClone(proposal[0]), label: 'Duplicate Chat' });
        const errors = nav.validateNavigationRegistry(proposal);
        let message = '';
        try { nav.assertValidNavigationRegistry(proposal); } catch (error) { message = error.message; }
        console.log(JSON.stringify({ errors, message }));
        """
    )

    assert any('Duplicate item id "home"' in error for error in result["errors"])
    assert any('Duplicate legacy id "rail-home"' in error for error in result["errors"])
    assert "Invalid navigation registry:" in result["message"]
