// Restia V2 navigation registry.
//
// This module is deliberately DOM-free. It describes the navigation contract
// shared by the expanded sidebar, icon rail, command palette, shortcuts,
// slash commands, deep links, and tool windows without taking over any of
// those integrations yet. Legacy element ids remain first-class metadata so
// the shell can migrate one surface at a time without changing behaviour.

const NAVIGATION_KINDS = Object.freeze(['destination', 'action', 'contextual']);
const NAVIGATION_SURFACES = Object.freeze([
  'rail',
  'sidebar',
  'mobile',
  'overflow',
  'command-palette',
  'route',
  'deep-link',
]);

function deepFreeze(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  Object.getOwnPropertyNames(value).forEach((key) => deepFreeze(value[key], seen));
  return Object.freeze(value);
}

function navigationItem(spec) {
  const legacyIds = spec.legacyIds || {};
  const visibility = spec.visibility || {};
  const capabilities = spec.capabilities || {};
  return {
    id: spec.id,
    aliases: spec.aliases || [],
    label: spec.label,
    shortLabel: spec.shortLabel || spec.label,
    description: spec.description || '',
    icon: spec.icon || spec.id,
    group: spec.group,
    order: Number.isInteger(spec.order) ? spec.order : 0,
    kind: spec.kind || 'destination',
    parentId: spec.parentId || null,
    route: spec.route || null,
    deepLinks: spec.deepLinks || [],
    surfaces: spec.surfaces || [],
    legacyIds: {
      rail: legacyIds.rail || [],
      sidebar: legacyIds.sidebar || [],
      auxiliary: legacyIds.auxiliary || [],
    },
    containerId: spec.containerId || null,
    modal: spec.modal || null,
    visibility: {
      defaultVisible: visibility.defaultVisible !== false,
      contextual: visibility.contextual === true,
      contextKey: visibility.contextKey || null,
      preferences: visibility.preferences || {},
    },
    capabilities: {
      featureFlags: capabilities.featureFlags || [],
      privileges: capabilities.privileges || [],
    },
    command: spec.command || null,
    shortcut: spec.shortcut || null,
    slash: spec.slash || null,
  };
}

export const NAVIGATION_GROUPS = deepFreeze([
  { id: 'home', label: 'Home', order: 10 },
  { id: 'work', label: 'Work', order: 20 },
  { id: 'knowledge', label: 'Knowledge', order: 30 },
  { id: 'learn', label: 'Learn', order: 40 },
  { id: 'connect', label: 'Connect', order: 50 },
  { id: 'ai-lab', label: 'AI Lab', order: 60 },
  { id: 'system', label: 'System', order: 70 },
  { id: 'quick-actions', label: 'Quick actions', order: 80, hidden: true },
]);

// V3 has one stable primary hierarchy on every adaptive surface. Canonical
// ids intentionally retain the V2/public route owners so existing commands,
// persisted recents, and deep links keep resolving while labels simplify.
export const PRIMARY_NAVIGATION_IDS = deepFreeze([
  'chat', 'home', 'inbox', 'life', 'search',
]);

export const NAVIGATION_ITEMS = deepFreeze([
  // Home and shell actions.
  navigationItem({
    id: 'home',
    aliases: ['today', 'mission-control'],
    label: 'Today',
    shortLabel: 'Today',
    description: 'Open the personal Mission Control overview.',
    icon: 'home',
    group: 'home',
    order: 5,
    route: '/today',
    surfaces: ['rail', 'sidebar', 'mobile', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-home'], sidebar: ['v2-home-nav'] },
    containerId: 'mission-control-workspace',
    command: {
      id: 'home', title: 'Today', hint: 'Open Mission Control', icon: '🏠',
      keywords: ['home', 'today', 'mission', 'control', 'overview'],
      triggerIds: ['v2-home-nav', 'rail-home'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'chat',
    aliases: ['chats', 'restia', 'assistant'],
    label: 'Restia',
    shortLabel: 'Restia',
    description: 'Return to a chat or open a completed background chat.',
    icon: 'chat',
    group: 'home',
    order: 10,
    route: '/',
    surfaces: ['rail', 'sidebar', 'mobile', 'route'],
    legacyIds: {
      rail: ['rail-restia', 'rail-chats'],
      sidebar: ['v3-restia-nav'],
      auxiliary: ['session-list'],
    },
    containerId: 'chat-container',
    visibility: {
      preferences: { sidebar: 'sessions-section' },
    },
    slash: {
      command: 'chats', subcommand: 'info', aliases: ['chat', 'session', 'sessions', 's'],
      usage: '/chats info', behavior: 'inspect',
    },
  }),
  navigationItem({
    id: 'new-chat',
    aliases: ['new-session'],
    label: 'New Chat',
    shortLabel: 'New',
    description: 'Start a fresh chat session.',
    icon: 'plus',
    group: 'home',
    order: 20,
    kind: 'action',
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: {
      rail: ['rail-new-session'],
      sidebar: ['sidebar-new-chat-btn'],
    },
    visibility: {
      preferences: { rail: 'rail-new-chat', sidebar: 'sidebar-new-chat' },
    },
    command: {
      id: 'new-chat', title: 'New Chat', hint: 'Start a fresh session', icon: '✨',
      keywords: ['new', 'session', 'conversation'], triggerIds: ['rail-new-session'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'new_session', defaultCombo: 'ctrl+alt+n' },
    slash: {
      command: 'chats', subcommand: 'new', aliases: ['new', 'create', 'mkdir'],
      usage: '/chats new [name]', behavior: 'create',
    },
  }),
  navigationItem({
    id: 'search',
    aliases: ['search-chats'],
    label: 'Search',
    description: 'Search chats and Life records.',
    icon: 'search',
    group: 'home',
    order: 30,
    kind: 'action',
    surfaces: ['rail', 'sidebar', 'mobile', 'command-palette'],
    legacyIds: { rail: ['rail-search-btn'], sidebar: ['sidebar-search-btn'] },
    visibility: { preferences: { sidebar: 'sidebar-search' } },
    command: {
      id: 'search', title: 'Search Restia', hint: 'Search chats and Life', icon: '🔎',
      keywords: ['find', 'search', 'history', 'life', 'records'], triggerIds: ['rail-search-btn'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'search', defaultCombo: 'ctrl+k' },
    slash: {
      command: 'find', subcommand: null, aliases: ['search-history'],
      usage: '/find query', behavior: 'search',
    },
  }),
  navigationItem({
    id: 'delete-session',
    aliases: ['delete-chat'],
    label: 'Delete Chat',
    shortLabel: 'Delete',
    description: 'Delete the current chat after confirmation.',
    icon: 'trash',
    group: 'home',
    order: 40,
    kind: 'action',
    surfaces: ['rail'],
    legacyIds: { rail: ['rail-delete-session'] },
    shortcut: { action: 'delete_session', defaultCombo: 'ctrl+alt+d' },
    slash: {
      command: 'chats', subcommand: 'delete', aliases: ['delete', 'del', 'rm'],
      usage: '/chats delete [id]', behavior: 'delete',
    },
  }),
  navigationItem({
    id: 'toggle-sidebar',
    label: 'Toggle Sidebar',
    description: 'Cycle the navigation shell between expanded, rail, and hidden states.',
    icon: 'sidebar',
    group: 'home',
    order: 50,
    kind: 'action',
    surfaces: ['sidebar', 'mobile'],
    legacyIds: {
      sidebar: ['sidebar-toggle-btn'],
      auxiliary: ['hamburger-btn', 'mobile-menu-btn'],
    },
    shortcut: { action: 'toggle_sidebar', defaultCombo: 'ctrl+b' },
    slash: {
      command: 'toggle', subcommand: 'sidebar', aliases: ['sidebar'],
      usage: '/toggle sidebar [full|mini|off]', behavior: 'toggle',
    },
  }),

  // Work.
  navigationItem({
    id: 'inbox',
    aliases: ['capture', 'universal-inbox'],
    label: 'Inbox',
    description: 'Capture, classify, process, and archive incoming items.',
    icon: 'inbox',
    group: 'work',
    order: 5,
    route: '/inbox',
    surfaces: ['rail', 'sidebar', 'mobile', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-inbox'], sidebar: ['tool-inbox-btn'] },
    containerId: 'inbox-workspace',
    command: {
      id: 'inbox', title: 'Inbox', hint: 'Open universal Inbox', icon: '▣',
      keywords: ['inbox', 'capture', 'triage', 'classify', 'process'],
      triggerIds: ['tool-inbox-btn', 'rail-inbox'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'life',
    aliases: ['life-os', 'life-workspace'],
    label: 'Life',
    description: 'Review the owner-scoped goals, projects, tasks, habits, people, and decisions that make up your life map.',
    icon: 'life',
    group: 'work',
    order: 7,
    route: '/life',
    surfaces: ['rail', 'sidebar', 'mobile', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-life'], sidebar: ['v3-life-nav'] },
    containerId: 'life-workspace',
    command: {
      id: 'life', title: 'Life', hint: 'Open your life map', icon: '◇',
      keywords: ['life', 'goals', 'projects', 'habits', 'decisions', 'people'],
      triggerIds: ['v3-life-nav', 'rail-life'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'projects',
    label: 'Projects',
    description: 'Open the full project and workflow workspace.',
    icon: 'projects',
    group: 'work',
    order: 10,
    route: '/projects',
    surfaces: ['rail', 'sidebar', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-projects'], sidebar: ['tool-projects-btn'] },
    containerId: 'projects-workspace',
    visibility: { preferences: { rail: 'tool-projects', sidebar: 'tool-projects' } },
    command: {
      id: 'projects', title: 'Projects', hint: 'Open project workspace', icon: '🗂️',
      keywords: ['projects', 'work', 'board', 'tasks', 'applications', 'labbook'],
      triggerIds: ['tool-projects-btn', 'rail-projects'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'tasks',
    label: 'Automations',
    description: 'Manage scheduled automations, runs, and activity.',
    icon: 'tasks',
    group: 'work',
    order: 20,
    route: '/tasks',
    deepLinks: ['#task-{id}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-tasks'], sidebar: ['tool-tasks-btn'] },
    modal: { ids: ['tasks-modal'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-tasks' } },
    command: {
      id: 'tasks', title: 'Automations', hint: 'Open automations', icon: '✅',
      keywords: ['automation', 'task', 'agent', 'scheduled'], triggerIds: ['tool-tasks-btn', 'rail-tasks'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_tasks', defaultCombo: '' },
    slash: { command: 'tasks', subcommand: null, aliases: [], usage: '/tasks', behavior: 'open' },
  }),
  navigationItem({
    id: 'calendar',
    label: 'Calendar',
    description: 'Open calendars, events, goals, and reminders.',
    icon: 'calendar',
    group: 'work',
    order: 30,
    route: '/calendar',
    deepLinks: ['#event-{uid}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-calendar'], sidebar: ['tool-calendar-btn'] },
    modal: { ids: ['calendar-modal'], manager: 'registered', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-calendar' } },
    command: {
      id: 'calendar', title: 'Calendar', hint: 'Open calendar', icon: '📅',
      keywords: ['events', 'schedule', 'caldav'], triggerIds: ['tool-calendar-btn', 'rail-calendar'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_calendar', defaultCombo: 'ctrl+alt+c' },
    slash: {
      command: 'event', subcommand: null, aliases: ['ev'],
      usage: '/event tomorrow 14:00 Team call', behavior: 'create',
    },
  }),
  navigationItem({
    id: 'todos',
    aliases: ['reminders'],
    label: 'To Do',
    description: 'Open reminders and checklists.',
    icon: 'todo',
    group: 'work',
    order: 40,
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: { rail: ['rail-todos'], sidebar: ['tool-todos-btn'] },
    modal: { ids: ['todos-panel'], manager: 'auto', kind: 'panel' },
    visibility: { preferences: { sidebar: 'tool-todos' } },
    command: {
      id: 'todos', title: 'Reminders & Todos', hint: 'Open todos', icon: '☑️',
      keywords: ['todo', 'reminder'], triggerIds: ['tool-todos-btn', 'rail-todos'],
      afterTriggerId: null, handler: null,
    },
    slash: {
      command: 'todo', subcommand: null, aliases: ['td'],
      usage: '/todo Your task  ·  /todo list', behavior: 'create-or-list',
    },
  }),

  // Knowledge.
  navigationItem({
    id: 'library',
    label: 'Library',
    description: 'Browse chats, documents, archive, and research.',
    icon: 'library',
    group: 'knowledge',
    order: 10,
    route: '/library',
    surfaces: ['rail', 'sidebar', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-archive'], sidebar: ['tool-library-btn'] },
    modal: { ids: ['doclib-modal', 'library-modal'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-library' } },
    command: {
      // Preserve the palette's existing id/title even though this destination
      // is canonically Library. `documents` is public recent-history state.
      id: 'documents', title: 'Documents', hint: 'Open library', icon: '📄',
      keywords: ['docs', 'editor', 'writing'], triggerIds: ['tool-library-btn', 'rail-documents'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_library', defaultCombo: '' },
    slash: {
      command: 'library', subcommand: null, aliases: ['docs', 'documents'],
      usage: '/library', behavior: 'open',
    },
  }),
  navigationItem({
    id: 'documents',
    aliases: ['document-editor'],
    label: 'Documents',
    shortLabel: 'Docs',
    description: 'Open the current chat document editor.',
    icon: 'document',
    group: 'knowledge',
    order: 20,
    kind: 'contextual',
    deepLinks: ['#document-{id}'],
    surfaces: ['rail', 'overflow', 'deep-link'],
    legacyIds: {
      rail: ['rail-documents'],
      auxiliary: ['overflow-doc-btn', 'doc-indicator-btn'],
    },
    containerId: 'doc-editor-pane',
    visibility: {
      defaultVisible: false,
      contextual: true,
      contextKey: 'document-active-or-present',
      preferences: { overflow: 'doc-toggle-btn' },
    },
    capabilities: {
      featureFlags: ['document_editor'],
      privileges: ['can_use_documents'],
    },
    slash: {
      command: 'toggle', subcommand: 'doc', aliases: ['doc'],
      usage: '/toggle doc', behavior: 'toggle',
    },
  }),
  navigationItem({
    id: 'notes',
    label: 'Notes',
    description: 'Open notes, reminders, and quick captures.',
    icon: 'notes',
    group: 'knowledge',
    order: 30,
    route: '/notes',
    deepLinks: ['#note-{id}', '#open=notes&note={id}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-notes'], sidebar: ['tool-notes-btn'] },
    containerId: 'notes-pane',
    modal: { ids: ['notes-panel'], manager: 'registered', kind: 'panel' },
    visibility: { preferences: { sidebar: 'tool-notes' } },
    command: {
      id: 'notes', title: 'Notes', hint: 'Open notes', icon: '📝',
      keywords: ['note', 'memo'], triggerIds: ['tool-notes-btn', 'rail-notes'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_notes', defaultCombo: '' },
    slash: { command: 'notes', subcommand: null, aliases: [], usage: '/notes', behavior: 'open' },
  }),
  navigationItem({
    id: 'memory',
    aliases: ['brain'],
    label: 'Brain',
    description: 'Manage memories and skills.',
    icon: 'brain',
    group: 'knowledge',
    order: 40,
    route: '/memory',
    deepLinks: ['#skill-{name}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-memory'], sidebar: ['tool-memory-btn'] },
    modal: { ids: ['memory-modal'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-memory' } },
    capabilities: { privileges: ['can_manage_memory'] },
    command: {
      id: 'memory', title: 'Memory', hint: 'Open memory', icon: '🧠',
      keywords: ['memory', 'facts', 'recall'], triggerIds: ['tool-memory-btn', 'rail-memory'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_memory', defaultCombo: '' },
    slash: {
      command: 'brain', subcommand: null, aliases: ['memories'],
      usage: '/brain', behavior: 'open',
    },
  }),

  // Learn.
  navigationItem({
    id: 'study',
    label: 'Study Mode',
    shortLabel: 'Study',
    description: 'Open a durable AI-linked Study workspace.',
    icon: 'study',
    group: 'learn',
    order: 10,
    route: '/study',
    surfaces: ['rail', 'sidebar', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-study'], sidebar: ['tool-study-btn'] },
    containerId: 'study-panel',
    visibility: { preferences: { rail: 'tool-study', sidebar: 'tool-study' } },
    command: {
      id: 'study', title: 'Study Mode', hint: 'Open Study workspace', icon: '📚',
      keywords: ['study', 'learn', 'mastery', 'review', 'feynman'],
      triggerIds: ['tool-study-btn', 'rail-study'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'research',
    aliases: ['deep-research'],
    label: 'Deep Research',
    shortLabel: 'Research',
    description: 'Run and inspect multi-step research jobs.',
    icon: 'research',
    group: 'learn',
    order: 20,
    deepLinks: ['#research-{id}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'deep-link'],
    legacyIds: { rail: ['rail-research'], sidebar: ['tool-research-btn'], auxiliary: ['overflow-research-btn'] },
    modal: { ids: ['research-overlay'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-research', overflow: 'research-btn' } },
    capabilities: {
      featureFlags: ['deep_research'],
      privileges: ['can_use_research'],
    },
    command: {
      id: 'research', title: 'Deep Research', hint: 'Open research', icon: '🔬',
      keywords: ['research', 'web', 'report'], triggerIds: ['tool-research-btn', 'rail-research'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_research', defaultCombo: '' },
    slash: { command: 'research', subcommand: null, aliases: [], usage: '/research', behavior: 'open' },
  }),

  // Connect.
  navigationItem({
    id: 'messages',
    label: 'Messages',
    description: 'Open account-to-account conversations.',
    icon: 'messages',
    group: 'connect',
    order: 10,
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: { rail: ['rail-messages'], sidebar: ['tool-messages-btn'] },
    modal: { ids: ['messages-modal'], manager: 'registered', kind: 'modal' },
    command: {
      id: 'messages', title: 'Messages', hint: 'Open chats', icon: '💬',
      keywords: ['dm', 'chat', 'talk'], triggerIds: ['tool-messages-btn', 'rail-messages'],
      afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'email',
    label: 'Email',
    description: 'Open the connected email inbox.',
    icon: 'email',
    group: 'connect',
    order: 20,
    route: '/email',
    deepLinks: ['#email={folder}:{uid}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-email'], sidebar: ['email-section-title'] },
    modal: { ids: ['email-lib-modal'], manager: 'registered', kind: 'modal' },
    visibility: { preferences: { sidebar: 'email-section' } },
    command: {
      id: 'email', title: 'Email', hint: 'Open inbox', icon: '✉️',
      keywords: ['mail', 'inbox', 'imap'], triggerIds: ['rail-email', 'email-section-title'],
      afterTriggerId: null, handler: null,
    },
    slash: {
      command: 'email', subcommand: null, aliases: ['mail', 'inbox'],
      usage: '/email', behavior: 'open',
    },
  }),

  // AI Lab.
  navigationItem({
    id: 'compare',
    label: 'Compare',
    description: 'Compare model responses side by side.',
    icon: 'compare',
    group: 'ai-lab',
    order: 10,
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: { rail: ['rail-compare'], sidebar: ['tool-compare-btn'] },
    modal: { ids: ['compare-model-overlay'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-compare' } },
    command: {
      id: 'compare', title: 'Compare Models', hint: 'Open compare', icon: '⚖️',
      keywords: ['compare', 'models', 'test'], triggerIds: ['tool-compare-btn', 'rail-compare'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_compare', defaultCombo: '' },
    slash: { command: 'compare', subcommand: null, aliases: [], usage: '/compare', behavior: 'open' },
  }),
  navigationItem({
    id: 'cookbook',
    label: 'Cookbook',
    description: 'Discover, download, and serve models.',
    icon: 'cookbook',
    group: 'ai-lab',
    order: 20,
    route: '/cookbook',
    surfaces: ['rail', 'sidebar', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-cookbook'], sidebar: ['tool-cookbook-btn'] },
    modal: { ids: ['cookbook-modal'], manager: 'registered', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-cookbook' } },
    command: {
      id: 'cookbook', title: 'Cookbook', hint: 'Models & serving', icon: '📓',
      keywords: ['cookbook', 'models', 'download', 'serve'], triggerIds: ['tool-cookbook-btn', 'rail-cookbook'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_cookbook', defaultCombo: '' },
    slash: {
      command: 'cookbook', subcommand: null, aliases: ['cook'],
      usage: '/cookbook  ·  /cookbook serve qwen', behavior: 'open',
    },
  }),
  navigationItem({
    id: 'gallery',
    label: 'Gallery',
    description: 'Browse and edit generated or uploaded images.',
    icon: 'gallery',
    group: 'ai-lab',
    order: 30,
    route: '/gallery',
    deepLinks: ['#image-{id}'],
    surfaces: ['rail', 'sidebar', 'command-palette', 'route', 'deep-link'],
    legacyIds: { rail: ['rail-gallery'], sidebar: ['tool-gallery-btn'] },
    modal: { ids: ['gallery-modal'], manager: 'registered', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-gallery' } },
    capabilities: { featureFlags: ['gallery'] },
    command: {
      id: 'gallery', title: 'Gallery', hint: 'Open gallery', icon: '🖼️',
      keywords: ['images', 'photos', 'pictures'], triggerIds: ['tool-gallery-btn', 'rail-gallery'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_gallery', defaultCombo: '' },
    slash: { command: 'gallery', subcommand: null, aliases: ['photos'], usage: '/gallery', behavior: 'open' },
  }),

  // System destinations.
  navigationItem({
    id: 'theme',
    label: 'Theme',
    description: 'Open theme browsing and customization.',
    icon: 'theme',
    group: 'system',
    order: 10,
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: { rail: ['rail-theme'], sidebar: ['tool-theme-btn'], auxiliary: ['open-theme-btn'] },
    modal: { ids: ['theme-modal'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'tool-theme' } },
    command: {
      id: 'theme', title: 'Toggle Theme', hint: 'Light / dark', icon: '🌗',
      keywords: ['theme', 'dark', 'light', 'appearance'], triggerIds: ['tool-theme-btn', 'rail-theme'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'open_theme', defaultCombo: '' },
    slash: { command: 'theme', subcommand: null, aliases: [], usage: '/theme name', behavior: 'configure' },
  }),
  navigationItem({
    id: 'settings',
    label: 'Settings',
    description: 'Open Restia settings.',
    icon: 'settings',
    group: 'system',
    order: 20,
    surfaces: ['rail', 'sidebar', 'command-palette'],
    legacyIds: { rail: ['rail-settings'], sidebar: ['user-bar-settings'] },
    modal: { ids: ['settings-modal'], manager: 'auto', kind: 'modal' },
    visibility: { preferences: { sidebar: 'sidebar-settings-btn' } },
    command: {
      id: 'settings', title: 'Settings', hint: 'Open settings', icon: '⚙️',
      keywords: ['settings', 'preferences', 'config'], triggerIds: ['rail-settings', 'user-bar-settings'],
      afterTriggerId: null, handler: null,
    },
    shortcut: { action: 'settings', defaultCombo: 'ctrl+,' },
    slash: {
      command: 'settings', subcommand: null, aliases: ['cfg', 'preferences', 'config'],
      usage: '/settings [tab]', behavior: 'open',
    },
  }),
  navigationItem({
    id: 'activity',
    aliases: ['maintainer-center'],
    label: 'Activity',
    description: 'Open task activity, system health, and maintenance status.',
    icon: 'activity',
    group: 'system',
    order: 25,
    route: '/activity',
    surfaces: ['rail', 'sidebar', 'command-palette', 'route'],
    legacyIds: { rail: ['rail-activity'], sidebar: ['v2-activity-nav'] },
    command: {
      id: 'activity', title: 'Activity', hint: 'Open Activity Center', icon: '📊',
      keywords: ['activity', 'maintainer', 'health', 'status', 'jobs', 'runs'],
      triggerIds: ['v2-activity-nav', 'rail-activity'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'notifications',
    label: 'Notifications',
    description: 'Review communication, calendar, task, and background-work attention.',
    icon: 'bell',
    group: 'system',
    order: 24,
    kind: 'action',
    surfaces: ['sidebar', 'command-palette'],
    legacyIds: { sidebar: ['rail-notif-center'] },
    command: {
      id: 'notifications', title: 'Notifications', hint: 'Open attention center', icon: '🔔',
      keywords: ['notifications', 'attention', 'unread', 'alerts'],
      triggerIds: ['rail-notif-center'], afterTriggerId: null, handler: null,
    },
  }),
  navigationItem({
    id: 'profile',
    label: 'Profile',
    description: 'Open the Account view in Settings.',
    icon: 'profile',
    group: 'system',
    order: 30,
    kind: 'action',
    parentId: 'settings',
    surfaces: ['sidebar'],
    legacyIds: { sidebar: ['user-bar-profile'] },
    visibility: { preferences: { sidebar: 'user-bar' } },
  }),

  // Command-palette and inline navigation actions. These do not render as
  // top-level destinations, but keeping them here removes the need for a
  // second command registry during the staged migration.
  navigationItem({
    id: 'quick-note',
    label: 'Quick Note',
    description: 'Capture a note without leaving the current workspace.',
    icon: 'bolt',
    group: 'quick-actions',
    order: 10,
    kind: 'action',
    parentId: 'notes',
    surfaces: ['command-palette'],
    command: {
      id: 'quick-note', title: 'Quick Note', hint: 'Jot something instantly', icon: '⚡',
      keywords: ['quick', 'note', 'capture', 'jot', 'memo', 'write'], triggerIds: [],
      afterTriggerId: null, handler: 'quick-capture:note',
    },
    slash: { command: 'note', subcommand: null, aliases: ['n'], usage: '/note text', behavior: 'create' },
  }),
  navigationItem({
    id: 'quick-todo',
    label: 'Quick Todo',
    description: 'Capture a checklist without leaving the current workspace.',
    icon: 'bolt',
    group: 'quick-actions',
    order: 20,
    kind: 'action',
    parentId: 'todos',
    surfaces: ['command-palette'],
    command: {
      id: 'quick-todo', title: 'Quick Todo', hint: 'Capture a checklist', icon: '⚡',
      keywords: ['quick', 'todo', 'task', 'checklist', 'capture'], triggerIds: [],
      afterTriggerId: null, handler: 'quick-capture:todo',
    },
  }),
  navigationItem({
    id: 'new-message',
    label: 'New Message',
    description: 'Open Messages and start a conversation.',
    icon: 'send',
    group: 'quick-actions',
    order: 30,
    kind: 'action',
    parentId: 'messages',
    surfaces: ['command-palette'],
    command: {
      id: 'new-message', title: 'New Message', hint: 'Message someone', icon: '📨',
      keywords: ['dm', 'new', 'message', 'compose'], triggerIds: ['tool-messages-btn', 'rail-messages'],
      afterTriggerId: 'msg-newchat-btn', handler: null,
    },
  }),
  navigationItem({
    id: 'share-moment',
    label: 'Share a Moment',
    description: 'Open Messages and create a status photo.',
    icon: 'moment',
    group: 'quick-actions',
    order: 40,
    kind: 'action',
    parentId: 'messages',
    surfaces: ['command-palette'],
    command: {
      id: 'share-moment', title: 'Share a Moment', hint: 'Post a status photo', icon: '📸',
      keywords: ['moment', 'status', 'photo', 'bereal'], triggerIds: ['tool-messages-btn', 'rail-messages'],
      afterTriggerId: 'msg-moment-add', handler: null,
    },
  }),
  navigationItem({
    id: 'compose-email',
    label: 'Compose Email',
    description: 'Create a new email.',
    icon: 'compose',
    group: 'quick-actions',
    order: 50,
    kind: 'action',
    parentId: 'email',
    surfaces: ['sidebar'],
    legacyIds: { sidebar: ['email-compose-btn'] },
  }),
  navigationItem({
    id: 'new-document',
    label: 'New Document',
    description: 'Create a blank document in the Library.',
    icon: 'document-plus',
    group: 'quick-actions',
    order: 60,
    kind: 'action',
    parentId: 'library',
    surfaces: ['sidebar'],
    legacyIds: { sidebar: ['library-new-doc-btn'] },
  }),
  navigationItem({
    id: 'manage-chats',
    label: 'Manage Chats',
    description: 'Open the Library on its Chats view.',
    icon: 'library',
    group: 'quick-actions',
    order: 70,
    kind: 'action',
    parentId: 'library',
    surfaces: ['sidebar'],
    legacyIds: { sidebar: ['chats-library-btn'] },
  }),
  navigationItem({
    id: 'new-model-chat',
    label: 'New Model Chat',
    description: 'Start a chat with the selected model.',
    icon: 'model-chat',
    group: 'quick-actions',
    order: 80,
    kind: 'action',
    parentId: 'new-chat',
    surfaces: ['sidebar'],
    legacyIds: { sidebar: ['btn-model-chat'] },
    visibility: { preferences: { sidebar: 'models-section' } },
  }),
]);

// Semantic alias retained for integrations that prefer “registry” terminology.
export const NAVIGATION_REGISTRY = NAVIGATION_ITEMS;

function normalizeDomId(value) {
  const id = String(value || '').trim();
  return id.startsWith('#') ? id.slice(1) : id;
}

function normalizeRoute(value) {
  if (value == null || value === '') return null;
  try {
    const url = new URL(String(value), 'http://restia.local');
    let pathname = url.pathname || '/';
    if (pathname.length > 1) pathname = pathname.replace(/\/+$/, '');
    return pathname || '/';
  } catch (_) {
    return null;
  }
}

function uniqueStrings(values) {
  return [...new Set((values || []).filter((value) => typeof value === 'string' && value))];
}

/**
 * Validate registry structure and cross-item uniqueness.
 * Returns human-readable errors instead of throwing so tests and migrations
 * can audit a proposed registry before adopting it.
 */
export function validateNavigationRegistry(items = NAVIGATION_ITEMS, groups = NAVIGATION_GROUPS) {
  const errors = [];
  if (!Array.isArray(items)) return ['Registry must be an array.'];
  if (!Array.isArray(groups)) return ['Groups must be an array.'];

  const groupIds = new Set();
  groups.forEach((group, index) => {
    const prefix = `Group at index ${index}`;
    if (!group || typeof group !== 'object') { errors.push(`${prefix} must be an object.`); return; }
    if (!/^[a-z][a-z0-9-]*$/.test(group.id || '')) errors.push(`${prefix} has an invalid id.`);
    else if (groupIds.has(group.id)) errors.push(`Duplicate group id "${group.id}".`);
    else groupIds.add(group.id);
    if (typeof group.label !== 'string' || !group.label.trim()) errors.push(`${prefix} must have a label.`);
  });

  const itemIds = new Set();
  const names = new Map();
  const legacyIds = new Map();
  const routes = new Map();
  const deepLinks = new Map();
  const modalIds = new Map();
  const commandIds = new Map();
  const shortcutActions = new Map();
  const slashKeys = new Map();

  const claim = (map, key, owner, label) => {
    if (!key) return;
    if (map.has(key)) errors.push(`Duplicate ${label} "${key}" on "${map.get(key)}" and "${owner}".`);
    else map.set(key, owner);
  };

  items.forEach((item, index) => {
    const prefix = `Item at index ${index}`;
    if (!item || typeof item !== 'object') { errors.push(`${prefix} must be an object.`); return; }
    const id = item.id || '';
    if (!/^[a-z][a-z0-9-]*$/.test(id)) errors.push(`${prefix} has an invalid id.`);
    else if (itemIds.has(id)) errors.push(`Duplicate item id "${id}".`);
    else itemIds.add(id);
    claim(names, id, id || prefix, 'item name');

    if (typeof item.label !== 'string' || !item.label.trim()) errors.push(`Item "${id}" must have a label.`);
    if (!groupIds.has(item.group)) errors.push(`Item "${id}" references unknown group "${item.group}".`);
    if (!NAVIGATION_KINDS.includes(item.kind)) errors.push(`Item "${id}" has invalid kind "${item.kind}".`);
    if (!Number.isInteger(item.order)) errors.push(`Item "${id}" must have an integer order.`);

    if (!Array.isArray(item.aliases)) errors.push(`Item "${id}" aliases must be an array.`);
    else item.aliases.forEach((alias) => {
      if (!/^[a-z][a-z0-9-]*$/.test(alias || '')) errors.push(`Item "${id}" has invalid alias "${alias}".`);
      else claim(names, alias, id, 'item name');
    });

    if (!Array.isArray(item.surfaces)) errors.push(`Item "${id}" surfaces must be an array.`);
    else {
      item.surfaces.forEach((surface) => {
        if (!NAVIGATION_SURFACES.includes(surface)) errors.push(`Item "${id}" has unknown surface "${surface}".`);
      });
      if (uniqueStrings(item.surfaces).length !== item.surfaces.length) errors.push(`Item "${id}" has duplicate surfaces.`);
    }

    if (item.route != null) {
      const normalized = normalizeRoute(item.route);
      if (!normalized || normalized !== item.route) errors.push(`Item "${id}" has non-canonical route "${item.route}".`);
      else claim(routes, normalized, id, 'route');
    }

    if (!Array.isArray(item.deepLinks)) errors.push(`Item "${id}" deepLinks must be an array.`);
    else item.deepLinks.forEach((pattern) => {
      if (typeof pattern !== 'string' || !pattern.startsWith('#')) {
        errors.push(`Item "${id}" has invalid deep-link template "${pattern}".`);
      } else {
        claim(deepLinks, pattern, id, 'deep-link template');
      }
    });

    for (const surface of ['rail', 'sidebar', 'auxiliary']) {
      const ids = item.legacyIds?.[surface];
      if (!Array.isArray(ids)) { errors.push(`Item "${id}" legacyIds.${surface} must be an array.`); continue; }
      ids.forEach((legacyId) => {
        if (!/^[A-Za-z][A-Za-z0-9_:-]*$/.test(legacyId || '')) {
          errors.push(`Item "${id}" has invalid legacy id "${legacyId}".`);
        } else {
          claim(legacyIds, legacyId, id, 'legacy id');
        }
      });
    }

    if (item.modal != null) {
      if (!Array.isArray(item.modal.ids) || item.modal.ids.length === 0) errors.push(`Item "${id}" modal.ids must be non-empty.`);
      else item.modal.ids.forEach((modalId) => claim(modalIds, modalId, id, 'modal id'));
      if (!['registered', 'auto', 'none'].includes(item.modal.manager)) errors.push(`Item "${id}" has invalid modal manager.`);
      if (!['modal', 'panel'].includes(item.modal.kind)) errors.push(`Item "${id}" has invalid modal kind.`);
    }

    if (item.command != null) {
      if (!/^[a-z][a-z0-9-]*$/.test(item.command.id || '')) errors.push(`Item "${id}" has an invalid command id.`);
      else claim(commandIds, item.command.id, id, 'command id');
      if (!Array.isArray(item.command.keywords)) errors.push(`Item "${id}" command keywords must be an array.`);
      if (!Array.isArray(item.command.triggerIds)) errors.push(`Item "${id}" command triggerIds must be an array.`);
      if (!item.command.handler && (!item.command.triggerIds || item.command.triggerIds.length === 0)) {
        errors.push(`Item "${id}" command needs triggerIds or a handler.`);
      }
    }

    if (item.shortcut != null) {
      if (!/^[a-z][a-z0-9_]*$/.test(item.shortcut.action || '')) errors.push(`Item "${id}" has an invalid shortcut action.`);
      else claim(shortcutActions, item.shortcut.action, id, 'shortcut action');
      if (typeof item.shortcut.defaultCombo !== 'string') errors.push(`Item "${id}" shortcut defaultCombo must be a string.`);
    }

    if (item.slash != null) {
      if (!/^[a-z][a-z0-9-]*$/.test(item.slash.command || '')) errors.push(`Item "${id}" has an invalid slash command.`);
      const slashKey = `${item.slash.command || ''}:${item.slash.subcommand || ''}`;
      claim(slashKeys, slashKey, id, 'slash target');
      if (!Array.isArray(item.slash.aliases)) errors.push(`Item "${id}" slash aliases must be an array.`);
      if (typeof item.slash.usage !== 'string' || !item.slash.usage.startsWith('/')) errors.push(`Item "${id}" has invalid slash usage.`);
    }

    for (const [gateType, gates] of Object.entries(item.capabilities || {})) {
      if (!['featureFlags', 'privileges'].includes(gateType) || !Array.isArray(gates)) {
        errors.push(`Item "${id}" has invalid capability metadata.`);
      } else if (gates.some((gate) => typeof gate !== 'string' || !gate)) {
        errors.push(`Item "${id}" has an invalid ${gateType} gate.`);
      }
    }
  });

  items.forEach((item) => {
    if (item?.parentId && !itemIds.has(item.parentId)) {
      errors.push(`Item "${item.id}" references unknown parent "${item.parentId}".`);
    }
  });
  return errors;
}

export function assertValidNavigationRegistry(items = NAVIGATION_ITEMS, groups = NAVIGATION_GROUPS) {
  const errors = validateNavigationRegistry(items, groups);
  if (errors.length) throw new Error(`Invalid navigation registry:\n- ${errors.join('\n- ')}`);
  return true;
}

assertValidNavigationRegistry();

const _byName = new Map();
const _byLegacyId = new Map();
const _byRoute = new Map();
const _byModalId = new Map();

for (const item of NAVIGATION_ITEMS) {
  _byName.set(item.id, item);
  item.aliases.forEach((alias) => _byName.set(alias, item));
  for (const ids of Object.values(item.legacyIds)) ids.forEach((id) => _byLegacyId.set(id, item));
  if (item.route) _byRoute.set(item.route, item);
  item.modal?.ids.forEach((id) => _byModalId.set(id, item));
}

/** Resolve a canonical item id or declared alias. */
export function getNavigationItem(idOrAlias) {
  return _byName.get(String(idOrAlias || '').trim().toLowerCase()) || null;
}

/** Resolve an existing DOM id (with or without a leading #). */
export function findNavigationItemByLegacyId(id) {
  return _byLegacyId.get(normalizeDomId(id)) || null;
}

/** Resolve a pathname, relative URL, or absolute URL. Query/hash are ignored. */
export function findNavigationItemByRoute(pathOrUrl) {
  const route = normalizeRoute(pathOrUrl);
  return route ? (_byRoute.get(route) || null) : null;
}

/** Resolve the navigation owner of a modal/panel id. */
export function findNavigationItemByModalId(id) {
  return _byModalId.get(normalizeDomId(id)) || null;
}

function compileDeepLinkTemplate(template) {
  const names = [];
  const chunks = String(template).split(/\{([a-z][a-z0-9_]*)\}/gi);
  let source = '^';
  chunks.forEach((chunk, index) => {
    if (index % 2) {
      names.push(chunk);
      source += '([^&#]+?)';
    } else {
      source += chunk.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }
  });
  source += '$';
  return { names, regex: new RegExp(source) };
}

const _deepLinkMatchers = NAVIGATION_ITEMS.flatMap((item) => item.deepLinks.map((pattern) => ({
  item,
  pattern,
  ...compileDeepLinkTemplate(pattern),
})));

/**
 * Match a Restia entity hash. Returns the owner, template, and decoded params,
 * or null when the hash is not a registered deep link.
 */
export function matchNavigationDeepLink(hash) {
  let candidate = String(hash || '').trim();
  if (!candidate) return null;
  if (!candidate.startsWith('#')) {
    try { candidate = new URL(candidate, 'http://restia.local').hash; } catch (_) { return null; }
  }
  for (const matcher of _deepLinkMatchers) {
    const match = matcher.regex.exec(candidate);
    if (!match) continue;
    const params = {};
    matcher.names.forEach((name, index) => {
      const raw = match[index + 1];
      try { params[name] = decodeURIComponent(raw); } catch (_) { params[name] = raw; }
    });
    return Object.freeze({ item: matcher.item, pattern: matcher.pattern, params: Object.freeze(params) });
  }
  return null;
}

function readGate(source, key, strict) {
  if (!source || typeof source !== 'object' || !(key in source)) return strict ? false : null;
  return source[key] !== false;
}

function readContextFlag(contexts, key) {
  if (!key || contexts == null) return null;
  if (contexts instanceof Set) return contexts.has(key);
  if (Array.isArray(contexts)) return contexts.includes(key);
  if (typeof contexts === 'object' && key in contexts) return contexts[key] === true;
  return null;
}

/**
 * Evaluate capability gates and surface visibility without reading global DOM
 * state. Missing feature/privilege values are allowed by default so async
 * startup preserves today's behaviour; pass `{ strict: true }` to fail closed.
 */
export function getNavigationAvailability(itemOrId, context = {}) {
  const item = typeof itemOrId === 'string' ? getNavigationItem(itemOrId) : itemOrId;
  if (!item) return Object.freeze({ available: false, visible: false, reasons: Object.freeze(['unknown-item']) });

  const reasons = [];
  const strict = context.strict === true;
  const features = context.features || context.featureFlags;
  const privileges = context.privileges;
  for (const key of item.capabilities.featureFlags) {
    if (readGate(features, key, strict) === false) reasons.push(`feature:${key}`);
  }
  for (const key of item.capabilities.privileges) {
    if (readGate(privileges, key, strict) === false) reasons.push(`privilege:${key}`);
  }

  const available = reasons.length === 0;
  let visible = available && item.visibility.defaultVisible;
  const surface = context.surface || null;
  if (item.visibility.contextual) {
    const contextFlag = readContextFlag(context.contexts, item.visibility.contextKey);
    if (context.includeContextual === true || contextFlag === true) visible = available;
    else if (context.includeContextual === false || contextFlag === false || contextFlag == null) {
      visible = false;
      reasons.push(`context:${item.visibility.contextKey || item.id}`);
    }
  }

  // Apply the surface restriction after contextual overrides so
  // `includeContextual` can reveal a contextual rail item, but can never make
  // it appear on a surface it does not support.
  if (surface && !item.surfaces.includes(surface)) {
    visible = false;
    reasons.push(`surface:${surface}`);
  }

  const preferenceKey = surface ? item.visibility.preferences[surface] : null;
  if (preferenceKey && context.preferences?.[preferenceKey] === false) {
    visible = false;
    reasons.push(`preference:${preferenceKey}`);
  }
  return Object.freeze({ available, visible, reasons: Object.freeze(uniqueStrings(reasons)) });
}

export function isNavigationItemAvailable(itemOrId, context = {}) {
  return getNavigationAvailability(itemOrId, context).available;
}

export function isNavigationItemVisible(itemOrId, context = {}) {
  return getNavigationAvailability(itemOrId, context).visible;
}

/** Return registry items in stable group/item order, optionally filtered. */
export function getNavigationItems(options = {}) {
  const groupOrder = new Map(NAVIGATION_GROUPS.map((group) => [group.id, group.order]));
  const hiddenGroups = new Set(NAVIGATION_GROUPS.filter((group) => group.hidden).map((group) => group.id));
  // Preserve the top-level surface filter when callers keep runtime gates in
  // a nested `context` object. Otherwise surface-specific preferences would
  // be silently skipped by the availability evaluator.
  const context = options.context
    ? { ...options.context, surface: options.surface || options.context.surface || null }
    : options;
  return NAVIGATION_ITEMS
    .filter((item) => !options.group || item.group === options.group)
    .filter((item) => !options.kind || item.kind === options.kind)
    .filter((item) => !options.surface || item.surfaces.includes(options.surface))
    // Hidden groups are metadata-only in the grouped sidebar. Surface-specific
    // consumers (for example the command palette) still receive their items.
    .filter((item) => options.includeHidden === true || options.surface || options.group || !hiddenGroups.has(item.group))
    .filter((item) => options.includeUnavailable === true || isNavigationItemAvailable(item, context))
    .filter((item) => options.includeHidden === true || isNavigationItemVisible(item, context))
    .slice()
    .sort((a, b) => (groupOrder.get(a.group) - groupOrder.get(b.group)) || (a.order - b.order) || a.id.localeCompare(b.id));
}

/**
 * Return legacy activation ids in preferred surface order. This is useful for
 * staged adapters that still dispatch clicks into the old shell.
 */
export function getLegacyTriggerIds(itemOrId, surfaceOrder = ['sidebar', 'rail', 'auxiliary']) {
  const item = typeof itemOrId === 'string' ? getNavigationItem(itemOrId) : itemOrId;
  if (!item) return [];
  const ids = [];
  for (const surface of surfaceOrder) {
    if (Array.isArray(item.legacyIds[surface])) ids.push(...item.legacyIds[surface]);
  }
  return uniqueStrings(ids);
}

export default NAVIGATION_ITEMS;
