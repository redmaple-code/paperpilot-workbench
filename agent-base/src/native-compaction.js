import { AgentBaseError } from './shared.js';
import { summarizeHistory, SUMMARY_INSTRUCTIONS } from './compaction.js';
import { upstreamModule } from './upstream.js';

export async function compactNativeSession(controls, initialPreparation, model, customInstructions, signal, reason) {
  if (!model || !controls) throw new AgentBaseError('configuration_required', 'Compaction requires an active model and run');
  const session = controls.session;
  const auth = await session._getSummarizationRequestAuth(model, signal);
  model = auth.model;
  const branchEntries = session.sessionManager.getBranch();
  const { prepareCompaction, completeSummarization } = await upstreamModule('core/compaction/compaction.js');
  const { buildSessionProjection } = await upstreamModule('core/session-manager.js');
  const { convertToLlm } = await upstreamModule('core/messages.js');
  const { computeFileLists, formatFileOperations, serializeConversation } = await upstreamModule('core/compaction/utils.js');
  const estimate = controls.ledger.options.estimateTokens;
  const window = model.contextWindow || 32768;
  // Keep recent context proportional to the selected model, including after a model switch.
  const preparation = prepareCompaction(branchEntries, { ...initialPreparation.settings,
    keepRecentTokens: Math.min(initialPreparation.settings.keepRecentTokens, Math.floor(window / 4)) }) || initialPreparation;
  const projection = buildSessionProjection(branchEntries);
  const first = projection.entries.findIndex(entry => entry.sourceEntry.id === preparation.firstKeptEntryId);
  if (first < 0) throw new AgentBaseError('compaction_failed', 'Retained session boundary is missing');
  const retained = projection.entries.slice(first).flatMap(entry => entry.messages);
  const files = computeFileLists(preparation.fileOps);
  const annotation = formatFileOperations(files.readFiles, files.modifiedFiles);
  const fixedContext = { messages: [
    { role: 'system', content: controls.session.systemPrompt, timestamp: 0 },
    ...retained,
  ] };
  const responseReserve = Math.min(controls.ledger.maxOutputTokens, model.maxTokens || 2048);
  const available = window - estimate(fixedContext) - estimate({ messages: [{ role: 'user', content: annotation, timestamp: 0 }] })
    - responseReserve - Math.max(128, Math.ceil(window * 0.05));
  const maxTokens = Math.floor(Math.min(responseReserve, window / 4, available * 0.75));
  if (maxTokens < 1) throw new AgentBaseError('context_overflow', 'Required instructions and recent context exceed capacity; reduce the current input or select a larger model');
  const messages = [
    ...(preparation.previousSummary ? [{ role: 'user', content: [{ type: 'text', text: `Previous checkpoint\n${preparation.previousSummary}` }], timestamp: 0 }] : []),
    ...preparation.messagesToSummarize, ...preparation.turnPrefixMessages,
  ];
  const result = await summarizeHistory(messages, {
    model, estimate, signal, maxTokens,
    serialize: history => serializeConversation(convertToLlm(history)),
    instructions: [SUMMARY_INSTRUCTIONS, customInstructions].filter(Boolean).join('\n'),
    complete: (request, options) => completeSummarization(model, request, {
      ...options, apiKey: auth.apiKey, headers: auth.headers, env: auth.env, reasoning: auth.thinkingLevel,
    }, session.agent.streamFunction, session.settingsManager.getRetrySettings(),
    session._summarizationRetryCallbacks({ source: 'compaction', reason })),
  });
  const summary = result.text + annotation;
  const projected = { messages: [...fixedContext.messages,
    { role: 'user', content: [{ type: 'text', text: summary }], timestamp: 0 }] };
  if (estimate(projected) + responseReserve > window) {
    throw new AgentBaseError('context_overflow', 'Generated summary exceeds the available context; the original session is retained');
  }
  return { summary, firstKeptEntryId: preparation.firstKeptEntryId,
    tokensBefore: preparation.tokensBefore, usage: result.usage,
    details: { ...files, inputReduction: result.changes, implementation: 'redmaple-codex-style/1' } };
}
