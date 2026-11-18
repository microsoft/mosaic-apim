import { describe, expect, it } from 'vitest'
import { compatibleRequestAnswers, requestAnswerErrors, requestAnswers } from './request-form'
import type { RequestQuestionView } from './types'

describe('opaque question evidence', () => {
  it('does not confuse object-prototype IDs with a provided answer or lose their validation errors', () => {
    const questions: RequestQuestionView[] = ['constructor', '__proto__'].map(id => ({
      id, kind: 'shortText', label: 'Required context', required: true,
      helpText: null, maxLength: 200, options: [],
    }))
    expect(Object.keys(requestAnswerErrors(questions, {}))).toEqual(['constructor', '__proto__'])
    expect(requestAnswers(questions, {})).toEqual([])
    const values = { ['constructor']: 'Fictional purpose', ['__proto__']: 'Fictional context' }
    expect(requestAnswerErrors(questions, values)).toEqual({})
    expect(requestAnswers(questions, values)).toHaveLength(2)
    expect(compatibleRequestAnswers(questions, questions, {})).toEqual({})
    expect(compatibleRequestAnswers(questions, questions, values)).toEqual(values)
  })
})
