import { describe, expect, it } from 'vitest'
import { compatibleRequestAnswers, requestAnswerErrors, requestAnswers } from './request-form'
import type { RequestQuestionView } from './types'

describe('opaque question evidence', () => {
  const choice: RequestQuestionView = {
    id: 'uses', kind: 'multipleChoice', label: 'Use cases', required: true,
    helpText: null, maxLength: null, allowCustomAnswer: true,
    options: [{ id: 'search', label: 'Search' }, { id: 'summary', label: 'Summaries' }],
  }
  it('omits empty optional choices but requires text when Other is selected', () => {
    const empty = { uses: { optionIds: [] } }
    expect(requestAnswers([choice], empty)).toEqual([])
    expect(requestAnswerErrors([choice], empty)).toEqual({ uses: 'Choose at least one answer.' })
    expect(requestAnswerErrors([{ ...choice, required: false }], empty)).toEqual({})
    expect(requestAnswerErrors([choice], { uses: { optionIds: ['search'], customText: '  ' } }))
      .toEqual({ uses: 'Enter your custom answer or deselect Other.' })
    expect(requestAnswerErrors([choice], { uses: { optionIds: [], customText: 'x'.repeat(201) } }))
      .toEqual({ uses: 'Use at most 200 characters for Other.' })
  })
  it('retains compatible choices only and never reuses a renamed option or disabled write-in', () => {
    const values = { uses: { optionIds: ['search', 'summary'], customText: 'One more purpose' } }
    expect(compatibleRequestAnswers([choice], [choice], values)).toEqual(values)
    expect(compatibleRequestAnswers([choice], [{ ...choice, options: [{ id: 'search', label: 'New meaning' }, choice.options[1]] }], values)).toEqual({})
    expect(compatibleRequestAnswers([choice], [{ ...choice, allowCustomAnswer: false }], values)).toEqual({})
  })
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
