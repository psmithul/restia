// Keep notification popovers inside the viewport regardless of which edge
// currently owns the sidebar/icon rail.
export function calculateNotificationPanelHorizontalPosition(
  anchorRect,
  viewportWidth,
  panelWidth,
  viewportPadding = 8,
) {
  const maxOffset = Math.max(viewportPadding, viewportWidth - panelWidth - viewportPadding);
  const clampOffset = (offset) => Math.max(viewportPadding, Math.min(maxOffset, Math.round(offset)));

  if (anchorRect.left >= viewportWidth / 2) {
    return {
      left: 'auto',
      right: `${clampOffset(viewportWidth - anchorRect.left + viewportPadding)}px`,
    };
  }

  return {
    left: `${clampOffset(anchorRect.right + viewportPadding)}px`,
    right: 'auto',
  };
}
