export function compareSnapshots(baseWorkspace, local, remoteWorkspace, {unknownBaseline = false} = {}) {
  const merged = structuredClone(remoteWorkspace);
  const conflicts = [];
  const collections = [];
  const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
  function field(object, name, before, left, right, label) {
    if (unknownBaseline && !equal(left, right)) conflicts.push({label, before, left, right, object, field: name});
    else if (equal(left, before)) object[name] = right;
    else if (equal(right, before) || equal(left, right)) object[name] = left;
    else conflicts.push({label, before, left, right, object, field: name});
  }
  for (const collection of ['nodes', 'edges', 'groups', 'pending_suggestions', 'paper_outline']) {
    const isGraph = ['nodes', 'edges', 'groups'].includes(collection);
    const key = collection === 'paper_outline' ? 'section_id' : 'id';
    const items = workspace => isGraph ? workspace.graph[collection] : workspace[collection];
    const base = new Map((items(baseWorkspace) || []).map(i => [i[key], i]));
    const mine = new Map((items(local) || []).map(i => [i[key], i]));
    const theirs = new Map((items(remoteWorkspace) || []).map(i => [i[key], i]));
    const output = {};
    for (const id of new Set([...base.keys(), ...mine.keys(), ...theirs.keys()])) {
      const before = base.get(id), left = mine.get(id), right = theirs.get(id);
      if (!before || !left || !right) {
        field(output, id, before, left, right, `${collection} / ${id}`);
      } else {
        const object = {};
        for (const name of new Set([...Object.keys(before), ...Object.keys(left), ...Object.keys(right)])) {
          field(object, name, before[name], left[name], right[name], `${left.label || left.relation || left.title || id} / ${name}`);
        }
        output[id] = object;
      }
    }
    collections.push({collection, isGraph, output});
  }
  const positions = {};
  const basePositions = baseWorkspace.layout.positions, localPositions = local.layout.positions, remotePositions = remoteWorkspace.layout.positions;
  for (const id of new Set([...Object.keys(basePositions), ...Object.keys(localPositions), ...Object.keys(remotePositions)])) {
    field(positions, id, basePositions[id], localPositions[id], remotePositions[id], `布局 / ${id}`);
  }
  return {conflicts, finish() {
    for (const {collection, isGraph, output} of collections) {
      (isGraph ? merged.graph : merged)[collection] = Object.values(output).filter(Boolean);
    }
    merged.layout.positions = Object.fromEntries(Object.entries(positions).filter(([, value]) => value !== undefined));
    return merged;
  }};
}

