"""Focused browser-module contracts for the Projects workspace.

The state tests import the shipped ES module in Node and exercise its pure
helpers. Static contracts cover the event paths that deliberately converge on
those helpers without maintaining a second copy of the production logic.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECTS_JS = ROOT / "static" / "js" / "projects.js"
PROJECTS_CSS = ROOT / "static" / "projects.css"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def run_node(body: str) -> dict:
    module_url = PROJECTS_JS.resolve().as_uri()
    script = f"import {{ __test }} from {json.dumps(module_url)};\n{body}"
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_move_transaction_reorders_and_rolls_back_exactly():
    result = run_node(
        """
        const items = [
          {id:'a', stage_id:'todo', position:0, title:'A'},
          {id:'b', stage_id:'doing', position:0, title:'B'},
          {id:'c', stage_id:'doing', position:1, title:'C'},
        ];
        const moved = __test.optimisticMove(items, 'a', 'doing', 1);
        const success = await __test.runMoveTransaction(
          items, 'a', 'doing', 1, async () => ({item:{id:'a', version:2}})
        );
        const failed = await __test.runMoveTransaction(
          items, 'a', 'doing', 1, async () => { throw new Error('conflict'); }
        );
        console.log(JSON.stringify({
          originalUnchanged: items[0].stage_id === 'todo',
          movedOrder: moved.filter(x => x.stage_id === 'doing').map(x => x.id),
          positions: moved.filter(x => x.stage_id === 'doing').map(x => x.position),
          successStage: success.items.find(x => x.id === 'a').stage_id,
          rollback: failed.items.map(x => [x.id, x.stage_id, x.position]),
          failed: !failed.ok,
        }));
        """
    )
    assert result == {
        "originalUnchanged": True,
        "movedOrder": ["b", "a", "c"],
        "positions": [0, 1, 2],
        "successStage": "doing",
        "rollback": [["a", "todo", 0], ["b", "doing", 0], ["c", "doing", 1]],
        "failed": True,
    }


def test_optimistic_move_keeps_archived_positions_out_of_active_ordering():
    result = run_node(
        """
        const items = [
          {id:'move', stage_id:'todo', position:0, archived:false},
          {id:'archived', stage_id:'doing', position:0, archived:true},
          {id:'active', stage_id:'doing', position:0, archived:false},
        ];
        const moved = __test.optimisticMove(items, 'move', 'doing', 1);
        console.log(JSON.stringify({
          active:moved.filter(x => !x.archived && x.stage_id === 'doing').map(x => [x.id,x.position]),
          archived:moved.find(x => x.id === 'archived').position,
        }));
        """
    )
    assert result == {
        "active": [["active", 0], ["move", 1]],
        "archived": 0,
    }


def test_request_gate_rejects_stale_project_responses():
    result = run_node(
        """
        const gate = __test.createRequestGate();
        const first = gate.next();
        const second = gate.next();
        const beforeInvalidate = [gate.current(first), gate.current(second)];
        gate.invalidate();
        console.log(JSON.stringify({
          beforeInvalidate,
          afterInvalidate: gate.current(second),
          generation: gate.value(),
        }));
        """
    )
    assert result == {
        "beforeInvalidate": [False, True],
        "afterInvalidate": False,
        "generation": 3,
    }


def test_attachment_queues_are_isolated_and_revoke_only_removed_task_urls():
    result = run_node(
        """
        const revoked = [];
        const store = new __test.AttachmentQueueStore(url => revoked.push(url));
        store.add('p1', 'task-a', {queueId:'a1', previewUrl:'blob:a'});
        store.add('p1', 'task-b', {queueId:'b1', previewUrl:'blob:b'});
        store.add('p2', 'task-a', {queueId:'c1', previewUrl:'blob:c'});
        store.remove('p1', 'task-a', 'a1');
        const isolated = [
          store.get('p1', 'task-a').length,
          store.get('p1', 'task-b').map(x => x.queueId),
          store.get('p2', 'task-a').map(x => x.queueId),
        ];
        store.clearAll();
        console.log(JSON.stringify({isolated, revoked: revoked.sort()}));
        """
    )
    assert result == {
        "isolated": [0, ["b1"], ["c1"]],
        "revoked": ["blob:a", "blob:b", "blob:c"],
    }


def test_normalizers_handle_nested_health_aliases_and_sanitize_css_colors():
    result = run_node(
        """
        const project = __test.normalizeProject({
          id: 9, name: '<img src=x>', key:'safe', color:'url(//tracker)', role:'viewer',
          overview:{total_items:8, done_items:3, overdue_items:2, blocked_items:1},
        });
        const board = __test.normalizeBoard({
          project,
          stages:[{id:1,name:'Review',category:'review',color:'javascript:bad',position:0}],
          items:[{
            id:2,key:'SAFE-2',title:'<script>alert(1)</script>',item_type:'epic',
            priority:'critical',assignee:'mits',stage_id:1,blocked_by_id:7,
          }],
          members:[{username:'mits'}],
          activity:[{id:3,event_type:'moved',work_item_id:2,text:'Moved'}],
        });
        console.log(JSON.stringify({
          project:{color:project.color,total:project.item_count,done:project.done_count,
                   overdue:project.overdue_count,blocked:project.blocked_count,role:project.role},
          stageColor:board.stages[0].color,
          task:{title:board.items[0].title,type:board.items[0].type,
                priority:board.items[0].priority,assignee:board.items[0].assignee_name,
                blocker:board.items[0].blocker_id},
          member:board.members[0],
          activity:{type:board.activity[0].type,item:board.activity[0].item_id},
        }));
        """
    )
    assert result["project"] == {
        "color": "#e06c75",
        "total": 8,
        "done": 3,
        "overdue": 2,
        "blocked": 1,
        "role": "viewer",
    }
    assert result["stageColor"] == "#7f849c"
    assert result["task"] == {
        "title": "<script>alert(1)</script>",
        "type": "epic",
        "priority": "critical",
        "assignee": "mits",
        "blocker": "7",
    }
    assert result["member"]["id"] == "mits"
    assert result["activity"] == {"type": "moved", "item": "2"}


def test_item_response_merge_applies_backend_aliases_and_preserves_loaded_detail():
    result = run_node(
        """
        const current = __test.normalizeItem({
          id:'task-1', item_type:'task', assignee:'alex', blocked_by_id:'task-2',
          checklist:[{id:'check-1',text:'Verify',done:false}],
          comments:[{id:'comment-1',body:'Context'}],
          attachments:[{id:'file-1',name:'report.pdf'}],
        });
        const merged = __test.mergeItemPayload(current, {
          id:'task-1', item_type:'subtask', assignee:null, blocked_by_id:null,
          parent_id:'task-3', version:4,
        });
        console.log(JSON.stringify({
          type:merged.type, assignee:merged.assignee_name, blocker:merged.blocker_id,
          blocked:merged.blocked, parent:merged.parent_id, version:merged.version,
          checklist:merged.checklist.length, comments:merged.comments.length,
          attachments:merged.attachments.length,
        }));
        """
    )
    assert result == {
        "type": "subtask",
        "assignee": "",
        "blocker": "",
        "blocked": False,
        "parent": "task-3",
        "version": 4,
        "checklist": 1,
        "comments": 1,
        "attachments": 1,
    }


def test_filters_and_mobile_stage_projection_are_deterministic():
    result = run_node(
        """
        const items = [
          __test.normalizeItem({id:1,key:'P-1',title:'Ship PDF',stage_id:'todo',priority:'high',
            labels:['launch'],assignee:'Mits',due_date:'2026-07-14',attachment_count:1}),
          __test.normalizeItem({id:2,key:'P-2',title:'Write notes',stage_id:'done',priority:'low',
            labels:['docs'],assignee:'Alex',attachment_count:0}),
        ];
        const filtered = __test.applyTaskFilters(items, {
          search:'pdf',priority:'high',type:'',label:'launch',assignee:'mits',
          due:'overdue',attachments:'with'
        }, new Date('2026-07-15T12:00:00'));
        const stages = [{id:'todo'},{id:'doing'},{id:'done'}];
        console.log(JSON.stringify({
          filtered:filtered.map(x => x.id),
          desktop:__test.visibleStagesForViewport(stages,false,'doing').map(x => x.id),
          mobile:__test.visibleStagesForViewport(stages,true,'doing').map(x => x.id),
          mobileFallback:__test.visibleStagesForViewport(stages,true,'missing').map(x => x.id),
        }));
        """
    )
    assert result == {
        "filtered": ["1"],
        "desktop": ["todo", "doing", "done"],
        "mobile": ["doing"],
        "mobileFallback": ["todo"],
    }


def test_project_health_excludes_done_and_archived_work_from_live_risk_counts():
    result = run_node(
        """
        const stages = [{id:'todo',category:'todo'},{id:'done',category:'done'}];
        const items = [
          __test.normalizeItem({id:'open',stage_id:'todo',due_date:'2026-07-14',blocked_by_id:'x'}),
          __test.normalizeItem({id:'soon',stage_id:'todo',due_date:'2026-07-18'}),
          __test.normalizeItem({id:'done',stage_id:'done',due_date:'2026-07-01',blocked_by_id:'x'}),
          __test.normalizeItem({id:'archived',stage_id:'todo',archived:true,due_date:'2026-07-01'}),
        ];
        console.log(JSON.stringify(
          __test.computeProjectHealth(items, stages, new Date('2026-07-15T12:00:00'))
        ));
        """
    )
    assert result == {"total": 3, "done": 1, "overdue": 1, "dueSoon": 1, "blocked": 1}
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "overviewMetric('Overdue', aggregate.overdue" in source
    assert "overviewMetric('Due soon', aggregate.dueSoon" in source
    assert "overviewMetric('Blocked', aggregate.blocked" in source
    assert "const dueSoonRows = Array.isArray(state.globalOverview?.due_soon)" in source


def test_attachment_picker_matches_backend_and_includes_mechanical_cad_formats():
    result = run_node(
        """
        const names = ['report.pdf','model.stl','assembly.step','surface.iges','data.json',
                       'legacy.doc','sheet.svg','macro.exe'];
        console.log(JSON.stringify(Object.fromEntries(names.map(name => [
          name, __test.isAcceptedAttachment({name, type:''})
        ]))));
        """
    )
    assert result == {
        "report.pdf": True,
        "model.stl": True,
        "assembly.step": True,
        "surface.iges": True,
        "data.json": True,
        "legacy.doc": False,
        "sheet.svg": False,
        "macro.exe": False,
    }


def test_task_context_drafts_permissions_and_activity_pages_are_scoped():
    result = run_node(
        """
        const item = __test.normalizeItem({
          id:'task-a', project_id:'project-1', title:'Original', description:'Initial',
          item_type:'story', priority:'high', stage_id:'todo', assignee:'mits',
          labels:['launch'], estimate_minutes:30,
        });
        const draft = __test.taskDraftFromItem(item);
        draft.values.title = 'Unsaved title';
        const context = {projectId:'project-1', itemId:'task-a', key:__test.taskScopeKey('project-1','task-a')};
        const merged = __test.mergeUniqueRows(
          [{id:'newest'},{id:'middle'}],
          [{id:'middle'},{id:'oldest'}],
        );
        const cursor = __test.activityCursorFromEntries([
          {id:'newest',created_at:'2026-07-15T12:00:00Z'},
          {id:'oldest',created_at:'2026-07-14T12:00:00Z'},
        ]);
        console.log(JSON.stringify({
          scope:context.key,
          current:__test.taskContextMatches(context,'project-1','task-a',true),
          wrongTask:__test.taskContextMatches(context,'project-1','task-b',true),
          closed:__test.taskContextMatches(context,'project-1','task-a',false),
          draft:{title:draft.values.title, original:item.title, stage:draft.values.stage_id,
                 labels:draft.values.labels, estimate:draft.values.estimate_minutes},
          merged:merged.map(row => row.id),
          cursor,
          deletes:{
            owner:__test.canDeleteOwnedResource('owner','','someone'),
            own:__test.canDeleteOwnedResource('editor','Mits','mits'),
            unknown:__test.canDeleteOwnedResource('editor','','mits'),
            colleague:__test.canDeleteOwnedResource('editor','mits','alex'),
          },
        }));
        """
    )
    assert result == {
        "scope": "project-1::task-a",
        "current": True,
        "wrongTask": False,
        "closed": False,
        "draft": {
            "title": "Unsaved title",
            "original": "Original",
            "stage": "todo",
            "labels": "launch",
            "estimate": "30",
        },
        "merged": ["newest", "middle", "oldest"],
        "cursor": "2026-07-14T12:00:00Z|oldest",
        "deletes": {"owner": True, "own": True, "unknown": False, "colleague": False},
    }


def test_task_drawer_mutations_keep_drafts_and_upload_batches_task_scoped():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "state.drawerDraft = taskDraftFromItem(detail)" in source
    assert "updateDrawerDraftField(target)" in source
    assert "{ deferMove: true }" in source
    assert "state.submittingTasks.add(context.key)" in source
    assert "state.submittingTasks.delete(context.key)" in source
    assert "if (!taskContextMatches(context) || !state.submittingTasks.has(context.key)) break;" in source
    assert "attachmentQueues.remove(context.projectId, context.itemId" in source
    assert "if (!taskContextMatches(context)) return;" in source
    assert "load-earlier-activity" in source
    assert "load-earlier-task-activity" in source
    assert "payload.next_before || null" in source
    assert "canDeleteComment(comment)" in source
    assert "canDeleteAttachment(attachment)" in source
    assert "preview-attachment" not in source
    assert "Preview could not open" not in source
    assert "renderNavigator();\n  renderCurrentView();" in source
    assert "if (!taskContextMatches(context)) return;" in source


def test_task_drawer_uses_sibling_forms_for_inline_mutations():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "id: 'projects-task-details-form'" in source
    assert "attrs: { form: 'projects-task-details-form' }" in source
    assert "const submit = refs.drawer?.querySelector(" in source
    assert "button[type=\"submit\"][form=\"projects-task-details-form\"]" in source
    assert "form.appendChild(renderChecklistSection" not in source
    assert "form.appendChild(renderDeliverablesSection" not in source
    assert "form.appendChild(renderCommentsSection" not in source
    assert "refs.drawer.append(\n    form,\n    renderChecklistSection" in source


def test_dom_contract_uses_one_move_path_and_safe_user_data_rendering():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "export async function moveTask(" in source
    assert re.search(
        r"case 'move-task-select':[\s\S]{0,220}await moveTask\(target\.dataset\.taskId",
        source,
    )
    assert re.search(r"async function onDrop[\s\S]+await moveTask\(taskId, stageId, position", source)
    assert "state.items = snapshot" in source
    assert "state.boardGate.current(token)" in source
    assert "state.activeProjectId !== id" in source
    assert "element.textContent = String(options.text)" in source
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "eval(" not in source
    assert "new AttachmentQueueStore()" in source
    assert re.search(
        r"async function createTask[\s\S]{0,500}projectPath\(projectId, '/items'\)"
        r"[\s\S]{0,500}if \(!state\.open \|\| state\.activeProjectId !== projectId\)",
        source,
    )
    assert "#projects-workspace" not in source  # id is created through DOM APIs, not HTML text
    for export in ("init", "open", "close", "toggle", "isOpen", "focus"):
        assert re.search(rf"export (?:async )?function {export}\(", source)


def test_attachment_and_backend_payload_contracts_are_scoped():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "formData.append('file', entry.file" in source
    assert "formData.append('kind'" in source
    assert "formData.append('submission_note'" in source
    assert "formData.append('transition_stage_id'" in source
    assert "/attachments/${encodeURIComponent(attachment.id)}`" in source
    assert "/api/projects/attachments/${encodeURIComponent(attachment.id)}/download" in source
    assert "item_type:" in source
    assert "blocked_by_id:" in source
    for clear_flag in (
        "clear_assignee",
        "clear_start_date",
        "clear_due_date",
        "clear_parent",
        "clear_blocked_by",
    ):
        assert clear_flag in source
    assert "projectPath(state.activeProjectId, '/members')" in source
    assert "`/members/${encodeURIComponent(username)}`" in source
    assert "projectPath(projectId, '/transfer')" in source
    assert "body: { username, version: projectVersion }" in source
    assert "if (!state.open || state.activeProjectId !== projectId)" in source
    assert "project.id === projectId ? { ...project, ...transferredProject }" in source


def test_workflow_mutations_match_backend_query_and_version_contracts():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "clear_wip_limit: clearWipLimit" in source
    assert "?move_to_stage_id=${moveTarget}" in source
    assert "method: 'DELETE', body: { move_to_stage_id" not in source
    assert "delete payload.stage_id" in source
    assert "stage.removeAttribute('data-action')" not in source
    assert "if (state.selectedItem?.id === taskKey) renderTaskDrawer()" in source
    assert "delete payload.template" in source


def test_archived_tasks_and_subtasks_have_complete_recovery_and_creation_paths():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "'/board?include_archived=true'" in source
    assert "function openArchivedTasksDialog()" in source
    assert "'restore-archived-task'" in source
    assert "async function restoreArchivedTask(taskId, control = null)" in source
    restore_source = source.split("async function restoreArchivedTask", 1)[1].split("function openTaskCreateDialog", 1)[0]
    assert "const projectId = state.activeProjectId;" in restore_source
    assert "if (!state.open || state.activeProjectId !== projectId) {" in restore_source
    assert "if (control?.isConnected) control.disabled = false;" in restore_source
    assert "dataset: { action: 'task-create-type' }" in source
    assert "itemType === 'subtask' && !parentId" in source
    assert "item.type !== 'subtask'" in source
    assert "member.role === 'owner' || member.role === 'editor'" in source


def test_css_is_namespaced_themed_accessible_and_mobile_is_single_column():
    css = PROJECTS_CSS.read_text(encoding="utf-8")
    assert ".projects-workspace" in css
    assert "var(--bg)" in css
    assert "var(--panel)" in css
    assert "var(--fg)" in css
    assert "var(--border)" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    mobile = css[css.index("@media (max-width: 768px)") :]
    assert ".projects-board" in mobile
    assert "display: block" in mobile
    assert "width: 100%" in mobile
    assert "min-height: 44px" in mobile
    assert ".projects-drawer" in mobile
    assert "height: 100dvh" in mobile
