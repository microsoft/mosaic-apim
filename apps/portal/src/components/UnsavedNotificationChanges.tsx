import { UnsavedChanges } from './UnsavedChanges'

export function UnsavedNotificationChanges({ dirty }: { dirty: boolean }) {
  return <UnsavedChanges dirty={dirty} title="Discard unsaved notification preferences?" />
}
