import { describe, expect, it } from 'vitest'
import { initialsFor } from './initials'

describe('initialsFor', () => {
  it('keeps the first letter of the first two words of an ordinary name', () => {
    expect(initialsFor('Ada Lovelace')).toBe('AL')
    expect(initialsFor('Mary Jane Watson')).toBe('MJ')
    expect(initialsFor('MOSAIC administrator')).toBe('MA')
    expect(initialsFor('ada lovelace')).toBe('AL')
    expect(initialsFor('Cher')).toBe('C')
  })

  it('uses letters and digits only', () => {
    expect(initialsFor('Name (Team)')).toBe('NT')
    expect(initialsFor('(Team) Name')).toBe('TN')
    expect(initialsFor('Surname, Given')).toBe('SG')
    expect(initialsFor('"Quoted" Name')).toBe('QN')
    expect(initialsFor('Name - Team')).toBe('NT')
    expect(initialsFor('Team 42')).toBe('T4')
  })

  it('handles letters outside ASCII', () => {
    expect(initialsFor('Élodie Ørsted')).toBe('ÉØ')
    expect(initialsFor('Ωmega (Δelta)')).toBe('ΩΔ')
  })

  it('returns nothing rather than punctuation when a name has no letters or digits', () => {
    expect(initialsFor('')).toBe('')
    expect(initialsFor('   ')).toBe('')
    expect(initialsFor('(-)')).toBe('')
  })
})
