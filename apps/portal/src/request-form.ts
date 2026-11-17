import type { RequestFormAnswer, RequestQuestionView } from './types'
import { ApiError } from './api'

export type RequestAnswerDraft = Record<string, string | boolean>
export const requestFormWarning = 'Do not include secrets, credentials, personal data or sensitive datasets. Describe code and intended usage at a high level.'

export function requestFormErrorMessage(error: unknown): string {
  if (error instanceof ApiError && Array.isArray(error.body?.detail)) {
    const messages = error.body.detail.flatMap(item =>
      typeof item === 'object' && item !== null && 'msg' in item && typeof item.msg === 'string'
        ? [item.msg] : [])
    if (messages.length) return messages.join(' ')
  }
  return error instanceof Error ? error.message : 'The request could not be confirmed. Retry or contact an administrator.'
}

export function requestAnswerErrors(questions: RequestQuestionView[], values: RequestAnswerDraft) {
  let errors: Record<string, string> = {}
  for (const question of questions) {
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    if (question.required && (value === undefined || value === '' ||
      typeof value === 'string' && !value.trim() || question.kind === 'acknowledgment' && value !== true)) {
      errors = { ...errors, [question.id]: question.kind === 'acknowledgment' ? 'Acknowledge this statement.' : 'Answer this required question.' }
    } else if (typeof value === 'string' && question.maxLength && Array.from(value.trim()).length > question.maxLength) {
      errors = { ...errors, [question.id]: `Use at most ${question.maxLength} characters.` }
    }
  }
  return errors
}

export function requestAnswers(questions: RequestQuestionView[], values: RequestAnswerDraft): RequestFormAnswer[] {
  return questions.flatMap(question => {
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    return value === undefined || typeof value === 'string' && !value.trim()
      ? [] : [{ questionId: question.id, value }]
  })
}

export function compatibleRequestAnswers(
  before: RequestQuestionView[], after: RequestQuestionView[], values: RequestAnswerDraft,
): RequestAnswerDraft {
  const previous = new Map(before.map(question => [question.id, question]))
  let kept: RequestAnswerDraft = {}
  for (const question of after) {
    const old = previous.get(question.id)
    const value = Object.hasOwn(values, question.id) ? values[question.id] : undefined
    if (!old || old.kind !== question.kind || value === undefined) continue
    if (question.kind === 'acknowledgment' && (old.label !== question.label || old.helpText !== question.helpText)) continue
    if (question.kind === 'singleChoice' && !question.options.some(option => option.id === value)) continue
    if (typeof value === 'string' && question.maxLength && Array.from(value.trim()).length > question.maxLength) continue
    kept = { ...kept, [question.id]: value }
  }
  return kept
}
