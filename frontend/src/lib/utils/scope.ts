/**
 * Display helpers for agent scope identifiers ("project/scope" strings).
 *
 * The backend returns scopes like "_vagrant/operator-handler" or
 * "myproj/agent-1". The UI splits them into a small project chip and a
 * large agent name, with _vagrant collapsed to the literal "handler".
 */

export function splitScopeId(id: string): { project: string; scope: string } {
  const i = id.indexOf('/');
  return i < 0
    ? { project: '', scope: id }
    : { project: id.slice(0, i), scope: id.slice(i + 1) };
}

export function agentDisplayName(id: string): string {
  const { project, scope } = splitScopeId(id);
  if (project === '_vagrant') return 'handler';
  return scope;
}

export function agentProjectLabel(id: string): string | null {
  const { project } = splitScopeId(id);
  if (!project || project === '_vagrant') return null;
  return project;
}
