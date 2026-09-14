import type { Employee } from "../model/types";

/**
 * Who may use something, as a row of checkboxes.
 *
 * Only the choosing. What the choice means - that an employee thereby gains a
 * service's tools and nothing is taken from anybody - is the core's rule, and
 * `locked` is the core's answer to who holds it through their own file, which
 * no box here can take away.
 */
export function EmployeeChoice({
  employees,
  chosen,
  locked = [],
  onChange,
  disabled,
}: {
  employees: Employee[];
  chosen: string[];
  locked?: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const toggle = (name: string) =>
    onChange(chosen.includes(name) ? chosen.filter((one) => one !== name) : [...chosen, name]);

  return (
    <fieldset className="employee-choice">
      <legend>Who can use it</legend>
      {employees.map((employee) => {
        const fixed = locked.includes(employee.name);
        return (
          <label key={employee.name} className="checkbox" title={employee.description}>
            <input
              type="checkbox"
              checked={fixed || chosen.includes(employee.name)}
              onChange={() => toggle(employee.name)}
              disabled={disabled || fixed}
            />
            <span>
              {employee.title || employee.name}
              <small>{fixed ? `${employee.name} · in its own file` : employee.name}</small>
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}
