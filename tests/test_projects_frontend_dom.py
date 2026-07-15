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


def test_instance_pairing_state_never_retains_a_redeemed_plaintext_code():
    result = run_node(
        """
        let state = __test.emptyInstancePairing();
        state = __test.reduceInstancePairing(state, {
          type:'creating', projectId:'project-1', role:'editor'
        });
        state = __test.reduceInstancePairing(state, {type:'created', pairing:{
          id:7, project_id:'project-1', role:'editor', status:'waiting',
          code:'one-time-secret', hub_url:'https://hub.example',
        }});
        const waiting = {...state};
        state = __test.reduceInstancePairing(state, {type:'status', pairing:{
          id:7, status:'paired', handle:'robot-lab', role:'editor',
        }});
        console.log(JSON.stringify({
          waiting:{status:waiting.status,code:waiting.code,projectId:waiting.projectId},
          redeemed:{status:state.status,code:state.code,handle:state.handle,
                    grant:state.grant,role:state.role},
        }));
        """
    )
    assert result == {
        "waiting": {
            "status": "waiting",
            "code": "one-time-secret",
            "projectId": "project-1",
        },
        "redeemed": {
            "status": "paired",
            "code": "",
            "handle": "robot-lab",
            "grant": None,
            "role": "editor",
        },
    }


def test_instance_pairing_cancel_clears_create_and_invite_busy_states():
    result = run_node(
        """
        let creating = __test.reduceInstancePairing(__test.emptyInstancePairing(), {
          type:'creating', projectId:'project-1', role:'editor'
        });
        creating = __test.reduceInstancePairing(creating, {type:'cancel-operation'});
        let paired = __test.reduceInstancePairing(__test.emptyInstancePairing(), {
          type:'created', pairing:{id:9, project_id:'project-1', status:'paired',
            role:'viewer', handle:'lab'}
        });
        paired = __test.reduceInstancePairing(paired, {type:'inviting'});
        const busy = paired.inviting;
        paired = __test.reduceInstancePairing(paired, {type:'cancel-operation'});
        console.log(JSON.stringify({
          createStatus:creating.status,
          createId:creating.id,
          busy,
          inviteStatus:paired.status,
          inviteId:paired.id,
          inviting:paired.inviting,
        }));
        """
    )
    assert result == {
        "createStatus": "idle",
        "createId": "",
        "busy": True,
        "inviteStatus": "paired",
        "inviteId": "9",
        "inviting": False,
    }


def test_unconfigured_home_link_is_an_empty_optional_surface():
    result = run_node(
        """
        const disconnected = Object.assign(new Error('link_not_connected'), {
          status:409, payload:{detail:'link_not_connected'},
        });
        const pending = Object.assign(new Error('link_pending'), {
          status:403, payload:{detail:'link_pending'},
        });
        const offline = Object.assign(new Error('Home server unreachable'), {status:502});
        console.log(JSON.stringify({
          disconnected:__test.homeLinkSurfaceError(disconnected,'fallback'),
          pending:__test.homeLinkSurfaceError(pending,'fallback'),
          offline:__test.homeLinkSurfaceError(offline,'fallback'),
          fallback:__test.homeLinkSurfaceError({},'fallback'),
        }));
        """
    )
    assert result == {
        "disconnected": "",
        "pending": "Home Link approval is pending.",
        "offline": "Home server unreachable",
        "fallback": "fallback",
    }


def test_linked_project_access_state_is_fail_closed():
    result = run_node(
        """
        console.log(JSON.stringify({
          local:__test.remoteProjectAccessState({role:'editor'},'local'),
          editor:__test.remoteProjectAccessState({role:'editor'},'home'),
          viewer:__test.remoteProjectAccessState({role:'viewer'},'home'),
          archived:__test.remoteProjectAccessState({role:'editor',archived:true},'home'),
          unavailable:__test.remoteProjectAccessState({role:'editor',remote_access_state:'unavailable'},'home'),
          removed:__test.remoteProjectAccessState({role:'editor',remote_access_state:'removed'},'home'),
        }));
        """
    )
    assert result == {
        "local": "active",
        "editor": "active",
        "viewer": "viewer",
        "archived": "archived",
        "unavailable": "unavailable",
        "removed": "removed",
    }


def test_activity_scope_rejects_stale_local_to_home_error_race():
    result = run_node(
        """
        const gate = __test.createRequestGate();
        const token = gate.next();
        const local = {
          gate, open:true, activeProjectId:'same-project', activeProjectSource:'local',
        };
        const switchedHome = {
          gate, open:true, activeProjectId:'same-project', activeProjectSource:'home',
        };
        const currentLocal = __test.activityRequestMatches(
          token, 'same-project', 'local', local,
        );
        const staleAfterSourceSwitch = __test.activityRequestMatches(
          token, 'same-project', 'local', switchedHome,
        );
        gate.invalidate();
        const staleAfterInvalidation = __test.activityRequestMatches(
          token, 'same-project', 'local', local,
        );
        console.log(JSON.stringify({
          currentLocal, staleAfterSourceSwitch, staleAfterInvalidation,
        }));
        """
    )
    assert result == {
        "currentLocal": True,
        "staleAfterSourceSwitch": False,
        "staleAfterInvalidation": False,
    }

    source = PROJECTS_JS.read_text(encoding="utf-8")
    select_source = source.split("async function selectProject", 1)[1].split(
        "function activityRequestMatches", 1
    )[0]
    assert "state.activityGate.invalidate();" in select_source
    assert "state.activityController?.abort();" in select_source
    activity_source = source.split("async function loadActivity", 1)[1].split(
        "export async function moveTask", 1
    )[0]
    assert activity_source.count(
        "if (!activityRequestMatches(token, projectId, projectSource)) return;"
    ) == 2


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
        const linkedWithoutRole = __test.normalizeProject({
          id:'linked', name:'Linked project', source:'home',
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
          linkedDefaultRole:linkedWithoutRole.role,
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
    assert result["linkedDefaultRole"] == "viewer"
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


def test_linked_projects_members_and_invitations_are_transport_scoped():
    result = run_node(
        """
        const local = __test.normalizeProject({id:'same',name:'Local',source:'local'});
        const linked = __test.normalizeProject({id:'same',name:'Linked',source:'home',role:'editor'});
        const archived = __test.normalizeProject({id:'old',name:'Old',source:'home',archived:true});
        const groups = __test.projectGroups([local,linked,archived]);
        const profile = __test.normalizeMember({username:'mits',kind:'profile',role:'editor'});
        const activeInstance = __test.normalizeMember({
          id:'grant-1',handle:'lab',kind:'instance',role:'editor',status:'accepted',version:4
        });
        const pendingInstance = __test.normalizeMember({
          grant_id:'grant-2',handle:'team',kind:'remote',role:'editor',status:'pending'
        });
        const remoteOwner = __test.normalizeMember({username:'instance',kind:'instance',role:'owner'});
        const hostRemote = __test.normalizeMember({
          id:'grant-4',username:'remote:grant-4',handle:'robotics-lab',kind:'instance',
          role:'editor',status:'active'
        });
        const invitation = __test.normalizeRemoteInvitation({
          id:'grant-3',role:'editor',version:2,
          project:{id:'p3',key:'LAB',name:'Lab build'}
        });
        const board = __test.normalizeBoard({project:{id:'same'},actor:{id:'grant-1',name:'This Restia'}}, 'home');
        console.log(JSON.stringify({
          keys:[__test.projectNavigatorKey(local),__test.projectNavigatorKey(linked)],
          parsed:__test.parseProjectNavigatorKey('home::same'),
          paths:[
            __test.projectPath('same','/board','local'),
            __test.projectPath('same','/board','home'),
          ],
          groups:{local:groups.local.map(x=>x.name),home:groups.home.map(x=>x.name)},
          members:{
            profile:{kind:profile.kind,status:profile.status,assignable:__test.isAssignableMember(profile)},
            active:{kind:activeInstance.kind,status:activeInstance.status,grant:activeInstance.grant_id,
                    version:activeInstance.version,assignable:__test.isAssignableMember(activeInstance)},
            pendingAssignable:__test.isAssignableMember(pendingInstance),
            ownerAssignable:__test.isAssignableMember(remoteOwner),
          },
          display:{
            host:__test.memberDisplayName('remote:grant-4',[hostRemote]),
            me:__test.memberDisplayName('me',[hostRemote]),
            owner:__test.memberDisplayName('instance',[remoteOwner]),
            hidden:__test.memberDisplayName('remote:missing',[]),
          },
          invitation:{grant:invitation.grant_id,project:invitation.project_name,
                      key:invitation.project_key,instance:invitation.instance_name},
          board:{source:board.project.source,actor:board.actor.username},
        }));
        """
    )
    assert result == {
        "keys": ["local::same", "home::same"],
        "parsed": {"source": "home", "id": "same"},
        "paths": ["/api/projects/same/board", "/api/homelink/projects/same/board"],
        "groups": {"local": ["Local"], "home": ["Linked"]},
        "members": {
            "profile": {"kind": "profile", "status": "active", "assignable": True},
            "active": {
                "kind": "instance",
                "status": "active",
                "grant": "grant-1",
                "version": 4,
                "assignable": True,
            },
            "pendingAssignable": False,
            "ownerAssignable": True,
        },
        "display": {
            "host": "robotics-lab",
            "me": "This Restia",
            "owner": "Owning Restia",
            "hidden": "Linked Restia",
        },
        "invitation": {
            "grant": "grant-3",
            "project": "Lab build",
            "key": "LAB",
            "instance": "Owning Restia",
        },
        "board": {"source": "home", "actor": "grant-1"},
    }


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


def test_remote_actor_identities_remain_raw_but_resolve_to_friendly_labels():
    result = run_node(
        """
        const members = [__test.normalizeMember({
          id:'6d4b',grant_id:'6d4b',username:'remote:6d4b',handle:'controls-team',
          kind:'instance',role:'editor',status:'active'
        })];
        const item = __test.normalizeItem({
          id:'task-1',assignee:'remote:6d4b',reporter:'remote:6d4b',
          comments:[{id:'c1',author:'remote:6d4b',body:'Ready'}],
          attachments:[{id:'a1',name:'report.pdf',uploader:'remote:6d4b'}],
          activity:[{id:'e1',actor:'remote:6d4b',summary:'Updated'}],
        });
        console.log(JSON.stringify({
          raw:{
            assignee:item.assignee_id,reporter:item.reporter,
            author:item.comments[0].author,uploader:item.attachments[0].uploader,
            actor:item.activity[0].actor,
          },
          labels:{
            assignee:__test.memberDisplayName(item.assignee_id,members),
            reporter:__test.memberDisplayName(item.reporter,members),
            author:__test.memberDisplayName(item.comments[0].author,members),
            uploader:__test.memberDisplayName(item.attachments[0].uploader,members),
            actor:__test.memberDisplayName(item.activity[0].actor,members),
          },
        }));
        """
    )
    assert result == {
        "raw": {
            "assignee": "remote:6d4b",
            "reporter": "remote:6d4b",
            "author": "remote:6d4b",
            "uploader": "remote:6d4b",
            "actor": "remote:6d4b",
        },
        "labels": {
            "assignee": "controls-team",
            "reporter": "controls-team",
            "author": "controls-team",
            "uploader": "controls-team",
            "actor": "controls-team",
        },
    }


def test_remote_redacted_dto_never_surfaces_opaque_grant_principals():
    result = run_node(
        """
        const grant = '11111111-1111-4111-8111-111111111111';
        const principal = `remote:${grant}`;
        // This is the actual remote serializer shape for another linked Restia:
        // its private handle is replaced with the opaque grant principal in
        // every identity field.
        const members = [__test.normalizeMember({
          id:grant, grant_id:grant, username:principal, handle:principal,
          name:principal, display_name:principal, kind:'instance',
          role:'editor', status:'active', version:3,
        })];
        const item = __test.normalizeItem({
          id:'task-1', assignee:principal, reporter:principal,
          comments:[{id:'c1',author:principal,body:'Ready'}],
          attachments:[{id:'a1',name:'report.pdf',uploader:principal}],
          activity:[{id:'e1',actor:principal,summary:'Updated'}],
        });
        console.log(JSON.stringify({
          raw:item.assignee_id,
          labels:{
            assignee:__test.memberDisplayName(item.assignee_id,members),
            reporter:__test.memberDisplayName(item.reporter,members),
            author:__test.memberDisplayName(item.comments[0].author,members),
            uploader:__test.memberDisplayName(item.attachments[0].uploader,members),
            actor:__test.memberDisplayName(item.activity[0].actor,members),
          },
        }));
        """
    )
    assert result == {
        "raw": "remote:11111111-1111-4111-8111-111111111111",
        "labels": {
            "assignee": "Linked Restia",
            "reporter": "Linked Restia",
            "author": "Linked Restia",
            "uploader": "Linked Restia",
            "actor": "Linked Restia",
        },
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
    assert "const dueSoonRows = project.source !== PROJECT_SOURCES.HOME" in source


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
        const context = {projectId:'project-1', itemId:'task-a', source:'local', key:__test.taskScopeKey('project-1','task-a')};
        const remoteContext = {projectId:'project-1', itemId:'task-a', source:'home'};
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
          remoteScope:__test.taskScopeKey('project-1','task-a','home'),
          remoteCurrent:__test.taskContextMatches(remoteContext,'project-1','task-a',true,'home'),
          wrongTransport:__test.taskContextMatches(remoteContext,'project-1','task-a',true,'local'),
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
        "remoteScope": "home::project-1::task-a",
        "remoteCurrent": True,
        "wrongTransport": False,
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
    assert "attachmentQueues.remove(projectQueueKey(context.projectId, context.source), context.itemId" in source
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
    assert "!activeProjectMatches(id, projectSource)" in source
    assert "element.textContent = String(options.text)" in source
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "eval(" not in source
    assert "new AttachmentQueueStore()" in source
    assert re.search(
        r"async function createTask[\s\S]{0,600}projectPath\(projectId, '/items', projectSource\)"
        r"[\s\S]{0,500}if \(!state\.open \|\| !activeProjectMatches\(projectId, projectSource\)\)",
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
    assert "if (!state.open || !activeProjectMatches(projectId, PROJECT_SOURCES.LOCAL))" in source
    assert "project.id === projectId && project.source === PROJECT_SOURCES.LOCAL" in source


def test_workflow_mutations_match_backend_query_and_version_contracts():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "clear_wip_limit: clearWipLimit" in source
    assert "?move_to_stage_id=${moveTarget}" in source
    assert "method: 'DELETE', body: { move_to_stage_id" not in source
    assert "delete payload.stage_id" in source
    assert "stage.removeAttribute('data-action')" not in source
    assert "if (state.selectedItem?.id === taskKey) renderTaskDrawer()" in source
    assert "delete payload.template" in source
    assert "projectPath(id, '', PROJECT_SOURCES.LOCAL)" in source
    assert "const value = member.username || member.id || member.name" in source
    assert "memberDisplayName(item.assignee_name || item.assignee_id)" in source
    assert "memberDisplayName(comment.author_name || comment.author)" in source
    assert "memberDisplayName(attachment.uploader_name || attachment.uploader)" in source
    assert "memberDisplayName(entry.actor_name || entry.actor)" in source


def test_archived_tasks_and_subtasks_have_complete_recovery_and_creation_paths():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "'/board?include_archived=true'" in source
    assert "function openArchivedTasksDialog()" in source
    assert "'restore-archived-task'" in source
    assert "async function restoreArchivedTask(taskId, control = null)" in source
    restore_source = source.split("async function restoreArchivedTask", 1)[1].split("function openTaskCreateDialog", 1)[0]
    assert "const projectId = state.activeProjectId;" in restore_source
    assert "if (!state.open || !activeProjectMatches(projectId, projectSource)) {" in restore_source
    assert "if (control?.isConnected) control.disabled = false;" in restore_source
    assert "dataset: { action: 'task-create-type' }" in source
    assert "itemType === 'subtask' && !parentId" in source
    assert "item.type !== 'subtask'" in source
    assert "state.members.filter(isAssignableMember)" in source


def test_cross_instance_dom_contract_is_accessible_and_never_expands_local_membership():
    source = PROJECTS_JS.read_text(encoding="utf-8")
    assert "projectPath(projectId, '/linked-instances', PROJECT_SOURCES.LOCAL)" in source
    assert "'/pairing-invitations'" in source
    assert "function reduceInstancePairing" in source
    assert "'invite-paired-instance'" in source
    assert "'review-instance-pairing'" in source
    assert "function invitePairedInstance" in source
    assert "pairing_invite_id: Number(pairing.id)" in source
    assert "verify the installation handle, then explicitly send its project invitation" in source
    assert "'/remote-invitations'" in source
    assert "`/remote-grants/${encodeURIComponent(id)}`" in source
    assert "'/api/homelink/projects?include_archived=true'" in source
    assert "'/api/homelink/projects/invitations'" in source
    assert "`/api/homelink/projects/invitations/${encodeURIComponent(id)}/respond`" in source
    assert "/api/homelink/projects/attachments/${encodeURIComponent(attachment.id)}/download" in source
    assert "renderNavigatorGroup('On this Restia'" in source
    assert "renderNavigatorGroup('Linked projects'" in source
    assert "renderIncomingInvitations({ idSuffix: '-mobile' })" in source
    assert "actionButton('Retry', 'retry-linked-projects'" in source
    retry_source = source.split("case 'retry-remote-invitations':", 1)[1].split(
        "case 'respond-project-invitation':", 1
    )[0]
    assert "case 'retry-linked-projects':" in retry_source
    assert "loadRemoteInvitations()" in retry_source
    assert "loadRemoteProjects()" in retry_source
    assert "loadProjects(" not in retry_source
    remote_refresh = source.split("async function loadRemoteProjects()", 1)[1].split(
        "async function respondToRemoteInvitation", 1
    )[0]
    assert "state.selectedItem" not in remote_refresh
    assert "selectProject(" not in remote_refresh
    assert "state.drawerDraft" not in remote_refresh
    assert "state.remoteProjectGate.next()" in remote_refresh
    assert "abortController('remoteProjectController')" in remote_refresh
    assert "state.projectGate.next()" not in remote_refresh
    assert "abortController('projectController')" not in remote_refresh
    assert "projects-linked-projects-error--mobile" in source
    invitation_response = source.split("async function respondToRemoteInvitation", 1)[1].split(
        "async function loadProjects", 1
    )[0]
    assert "loadRemoteProjects()" in invitation_response
    assert "loadProjects(" not in invitation_response
    full_load = source.split("async function loadProjects", 1)[1].split(
        "async function selectProject", 1
    )[0]
    assert "const remoteToken = state.remoteProjectGate.next();" in full_load
    assert "state.remoteProjectController?.abort()" in full_load
    assert "state.remoteProjectGate.current(remoteToken)" in full_load
    assert "function reconcileActiveRemoteProject" in source
    assert "remote_access_state: unavailable ? 'unavailable' : 'removed'" in source
    assert "Your unsaved task draft is retained here" in source
    assert "Current linked project" in source
    catalog_render = source.split("function renderRemoteCatalogUpdate", 1)[1].split(
        "async function loadRemoteProjects", 1
    )[0]
    assert "if (!reconciliation.viewChanged) return;" in catalog_render
    assert "const drawerScrollTop = refs.drawer?.scrollTop || 0;" in catalog_render
    assert "refs.drawer.scrollTop = drawerScrollTop;" in catalog_render
    assert "renderAll()" not in catalog_render
    assert "attrs: { role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' }" in source
    assert "whole project—its board, activity, and task files" in source
    assert "not one local profile" in source
    assert "state.members.filter(isAssignableMember)" in source
    assert "currentProjectActor()" in source
    remote_row = source.split("function renderRemoteMemberRow", 1)[1].split(
        "function approvedLinkedInstances", 1
    )[0]
    assert "remove-remote-grant" in remote_row
    assert "remote-grant-role" in remote_row
    assert "transfer-project" not in remote_row
    local_dialog = source.split("function openMembersDialog", 1)[1].split(
        "function renderMemberRow", 1
    )[0]
    assert "dataset: { form: 'member-add' }" in local_dialog
    assert "Local profiles" in local_dialog
    assert "content.append(renderLinkedMembersSection(), localSection)" in local_dialog
    assert "Project access" in local_dialog
    pairing_poll = source.split("async function pollInstancePairing", 1)[1].split(
        "async function createInstancePairing", 1
    )[0]
    assert "const changed = instancePairingFingerprint(next) !== previousFingerprint" in pairing_poll
    assert "if (changed)" in pairing_poll
    assert "renderAll()" not in pairing_poll
    pairing_create = source.split("async function createInstancePairing", 1)[1].split(
        "function pairingSetupText", 1
    )[0]
    assert "state.instancePairingGate.next()" in pairing_create
    assert "!state.open" in pairing_create
    assert "instancePairingCreateController" in pairing_create
    pairing_invite = source.split("async function invitePairedInstance", 1)[1].split(
        "async function inviteRemoteProjectMember", 1
    )[0]
    assert "state.instancePairingGate.next()" in pairing_invite
    assert "instancePairingInviteController" in pairing_invite
    assert "pairing_invite_id: Number(pairing.id)" in pairing_invite
    assert "!state.open" in pairing_invite
    pairing_revoke = source.split("async function revokeInstancePairing", 1)[1].split(
        "function reviewInstancePairing", 1
    )[0]
    assert "stopLinkedInstancesLoad()" in pairing_revoke
    assert "state.instancePairingGate.next()" in pairing_revoke
    assert "instancePairingRevokeController" in pairing_revoke
    assert "activeProjectMatches(pairing.projectId, PROJECT_SOURCES.LOCAL)" in pairing_revoke
    reset_pairing = source.split("case 'reset-instance-pairing':", 1)[1].split(
        "case 'manage-stages':", 1
    )[0]
    assert "stopInstancePairingOperations()" in reset_pairing
    assert "stopLinkedInstancesLoad()" in reset_pairing


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
    assert ".projects-invitations--mobile" in mobile
    assert ".projects-member-row--remote" in mobile
    assert ".projects-remote-invite" in mobile
    assert ".projects-pairing__steps li { overflow-wrap: anywhere; }" in css
