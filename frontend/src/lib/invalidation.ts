/** Collects query invalidations and runs them at most once per window.
 *
 * A scan that analyses a few thousand files publishes an event per file, and
 * each used to invalidate - and so refetch - the file list, the stats and more,
 * cancelling the previous refetch every time.  The server spent its time
 * answering requests nobody waited for.  Now events only mark keys as stale,
 * and one round of refetches follows at the end of the window.
 */
export type FlushTarget = string[] | "all";

export class InvalidationBatcher {
  private keys = new Set<string>();
  private everything = false;
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly flushFn: (target: FlushTarget) => void,
    private readonly windowMs = 1500,
  ) {}

  /** Mark query keys (first element of the query key) as stale. */
  add(keys: readonly string[]): void {
    if (!keys.length) return;
    keys.forEach((k) => this.keys.add(k));
    this.schedule();
  }

  /** Everything is stale - after a reconnect events may have been missed. */
  addAll(): void {
    this.everything = true;
    this.schedule();
  }

  get pending(): boolean {
    return this.timer !== undefined;
  }

  flush(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    const everything = this.everything;
    const keys = [...this.keys];
    this.everything = false;
    this.keys.clear();
    if (everything) this.flushFn("all");
    else if (keys.length) this.flushFn(keys);
  }

  dispose(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.everything = false;
    this.keys.clear();
  }

  private schedule(): void {
    // Throttle, not debounce: a steady stream of events must not postpone the
    // refresh forever, so the first event of a window fixes when it ends.
    if (this.timer !== undefined) return;
    this.timer = setTimeout(() => this.flush(), this.windowMs);
  }
}
