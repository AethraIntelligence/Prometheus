/**
 * The emergency stop, where the rest of what is done to this installation is.
 *
 * It used to float over every page in the top right corner. A brake nobody is
 * reaching for is a red mark in the corner of every conversation, and the one
 * screen it belonged on was the one about the machine rather than the work.
 * The press itself is unchanged - no confirmation, and resuming is its own
 * deliberate direction - and a stop set from anywhere still announces itself
 * on every screen as a banner, because "is work stopped" is not a question a
 * person should have to navigate to.
 */

import { EmergencyStop } from "../../../../features/emergency-stop";

export function StopSection() {
  return (
    <>
      <p className="lede">
        Stops everything at once: parked approvals are answered no, running tasks are signalled,
        and new work is refused until somebody resumes it. Actions already carried out are not
        undone.
      </p>

      <section className="panel">
        <div className="panel-head">
          <h2>Emergency stop</h2>
        </div>
        <div className="card">
          <EmergencyStop placement="section" />
        </div>
      </section>
    </>
  );
}
