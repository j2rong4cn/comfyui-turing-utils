/** Ordered writes retain failed edits until the same edit succeeds. */
export class SerialWrites {
  tail = Promise.resolve();
  pending = 0;
  errors = new Map();

  get idle() {
    return this.pending === 0 && this.errors.size === 0;
  }

  run(operation, key) {
    this.pending++;
    const task = this.tail
      .then(operation)
      .then(
        (value) => {
          if (key !== undefined) this.errors.delete(key);
          return value;
        },
        (error) => {
          if (key !== undefined) this.errors.set(key, error);
          throw error;
        },
      )
      .finally(() => this.pending--);
    // A failed operation must not prevent a later retry from entering the queue.
    // Its error remains observable to both the caller and settle().
    this.tail = task.catch(() => {});
    return task;
  }

  assertSaved() {
    if (this.errors.size) throw this.errors.values().next().value;
  }

  async drain() {
    let tail;
    do {
      tail = this.tail;
      await tail;
    } while (tail !== this.tail);
  }

  async settle() {
    await this.drain();
    this.assertSaved();
  }

  async discardFailures() {
    await this.drain();
    this.errors.clear();
  }
}
