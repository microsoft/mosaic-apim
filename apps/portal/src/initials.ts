const letterOrDigit = /[\p{L}\p{Nd}]/u

/**
 * Up to two initials for an avatar: the first letter or digit of each word in a display name.
 * Punctuation never becomes an initial, so "Name (Team)" gives "NT" rather than "N(".
 */
export function initialsFor(name: string): string {
  const initials: string[] = []
  for (const word of name.split(/\s+/)) {
    const initial = letterOrDigit.exec(word)?.[0]
    if (initial) {
      initials.push(initial)
    }
    if (initials.length === 2) {
      break
    }
  }
  return initials.join('').toUpperCase()
}
