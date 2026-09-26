import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SECRET_MASK } from "../lib/api";
import {
  Modal,
  NumberField,
  SecretField,
  TagListField,
  clampNumber,
  parseNumberInput,
  parseTagList,
  withCurrent,
} from "./ui";

function Tags({ initial = [] as string[], onChange = (_: string[]) => {} }) {
  const [values, setValues] = useState(initial);
  return (
    <>
      <TagListField
        values={values}
        onChange={(v) => {
          setValues(v);
          onChange(v);
        }}
        ariaLabel="Liste"
      />
      <output data-testid="value">{JSON.stringify(values)}</output>
    </>
  );
}

describe("TagListField", () => {
  it("parses only commas, so multi-word patterns survive", () => {
    expect(parseTagList(" deu, eng ,, */Season 1/* ")).toEqual(["deu", "eng", "*/Season 1/*"]);
  });

  it("lets commas and spaces be typed and commits on blur", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Tags onChange={onChange} />);
    const input = screen.getByLabelText("Liste");
    await user.type(input, "deu, eng, */Season 1/*");
    // Nothing is parsed while typing - the text stays exactly as typed.
    expect(input).toHaveProperty("value", "deu, eng, */Season 1/*");
    expect(onChange).not.toHaveBeenCalled();
    await user.tab();
    expect(onChange).toHaveBeenCalledWith(["deu", "eng", "*/Season 1/*"]);
    expect(screen.getByTestId("value").textContent).toBe('["deu","eng","*/Season 1/*"]');
  });

  it("commits on Enter", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Tags initial={["mkv"]} onChange={onChange} />);
    const input = screen.getByLabelText("Liste");
    await user.type(input, ", mp4{Enter}");
    expect(onChange).toHaveBeenCalledWith(["mkv", "mp4"]);
  });
});

function Num(props: { min?: number; max?: number; step?: number; initial?: number }) {
  const [value, setValue] = useState(props.initial ?? 10);
  return (
    <>
      <NumberField
        value={value}
        onChange={setValue}
        min={props.min}
        max={props.max}
        step={props.step}
        ariaLabel="Zahl"
      />
      <output data-testid="value">{value}</output>
    </>
  );
}

describe("NumberField", () => {
  it("parses German input and does not treat empty as 0", () => {
    expect(parseNumberInput("")).toBeNull();
    expect(parseNumberInput("-")).toBeNull();
    expect(parseNumberInput("1,5")).toBe(1.5);
    expect(parseNumberInput("-20")).toBe(-20);
    expect(clampNumber(25, { min: -20, max: 19, integer: true })).toBe(19);
    expect(clampNumber(2.6, { integer: true })).toBe(3);
  });

  it("restores the value when the field is left empty", async () => {
    const user = userEvent.setup();
    render(<Num initial={42} min={0} />);
    const input = screen.getByLabelText("Zahl");
    await user.clear(input);
    expect(screen.getByTestId("value").textContent).toBe("42");
    await user.tab();
    expect(input).toHaveProperty("value", "42");
    expect(screen.getByTestId("value").textContent).toBe("42");
  });

  it("allows typing a minus sign", async () => {
    const user = userEvent.setup();
    render(<Num initial={10} min={-20} max={19} />);
    const input = screen.getByLabelText("Zahl");
    await user.clear(input);
    await user.type(input, "-5");
    expect(input).toHaveProperty("value", "-5");
    expect(screen.getByTestId("value").textContent).toBe("-5");
  });

  it("clamps to min/max when leaving the field and says why meanwhile", async () => {
    const user = userEvent.setup();
    render(<Num initial={10} min={1} max={16} />);
    const input = screen.getByLabelText("Zahl");
    await user.clear(input);
    await user.type(input, "99");
    expect(screen.getByText("Erlaubt: 1 bis 16")).toBeTruthy();
    // "9" was valid on the way and passed on; "99" is not.
    expect(screen.getByTestId("value").textContent).toBe("9");
    await user.tab();
    expect(screen.getByTestId("value").textContent).toBe("16");
    expect(input).toHaveProperty("value", "16");
  });

  it("rejects letters", async () => {
    const user = userEvent.setup();
    render(<Num initial={5} />);
    const input = screen.getByLabelText("Zahl");
    await user.type(input, "abc");
    expect(input).toHaveProperty("value", "5");
  });
});

describe("SecretField", () => {
  function Secret({ initial, stored }: { initial: string; stored: boolean }) {
    const [value, setValue] = useState(initial);
    return (
      <>
        <SecretField value={value} stored={stored} onChange={setValue} ariaLabel="Schlüssel" />
        <output data-testid="value">{value}</output>
      </>
    );
  }

  it("shows a stored secret as a placeholder and keeps it when left empty", async () => {
    const user = userEvent.setup();
    render(<Secret initial={SECRET_MASK} stored />);
    const input = screen.getByLabelText("Schlüssel") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.placeholder).toMatch(/Gespeichert/);
    await user.type(input, "neu");
    expect(screen.getByTestId("value").textContent).toBe("neu");
    await user.clear(input);
    expect(screen.getByTestId("value").textContent).toBe(SECRET_MASK);
  });

  it("can delete a stored secret explicitly and undo that", async () => {
    const user = userEvent.setup();
    render(<Secret initial={SECRET_MASK} stored />);
    await user.click(screen.getByRole("button", { name: "Entfernen" }));
    expect(screen.getByTestId("value").textContent).toBe("");
    expect((screen.getByLabelText("Schlüssel") as HTMLInputElement).placeholder).toMatch(
      /entfernt/,
    );
    await user.click(screen.getByRole("button", { name: "Behalten" }));
    expect(screen.getByTestId("value").textContent).toBe(SECRET_MASK);
  });

  it("offers no delete button when nothing is stored", () => {
    render(<Secret initial="" stored={false} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("withCurrent", () => {
  it("adds an unknown stored value as its own option", () => {
    const options = [{ value: "a", label: "A" }];
    expect(withCurrent(options, "a")).toBe(options);
    expect(withCurrent(options, "old-model")).toEqual([
      { value: "a", label: "A" },
      { value: "old-model", label: "old-model (gespeichert)" },
    ]);
  });
});

describe("Modal", () => {
  function Harness({ onClose = () => {} }) {
    const [open, setOpen] = useState(false);
    const [count, setCount] = useState(0);
    return (
      <>
        <button onClick={() => setOpen(true)}>Öffnen</button>
        <Modal
          open={open}
          onClose={() => {
            onClose();
            setOpen(false);
          }}
          title="Titel"
          subtitle="Untertitel"
          footer={<button>Fuß</button>}
        >
          <input aria-label="Erstes Feld" />
          <button onClick={() => setCount((c) => c + 1)}>Mitte {count}</button>
        </Modal>
      </>
    );
  }

  it("is labelled by its title and focuses the first control", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Öffnen"));
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByRole("dialog", { name: "Titel" })).toBe(dialog);
    expect(document.activeElement).toBe(screen.getByLabelText("Erstes Feld"));
  });

  it("keeps Tab inside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Öffnen"));
    const dialog = screen.getByRole("dialog");
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("closes on Escape and returns focus to the opener", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const opener = screen.getByText("Öffnen");
    await user.click(opener);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("does not steal focus back on re-render", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Öffnen"));
    const middle = screen.getByRole("button", { name: /Mitte/ });
    // The click re-renders the parent, which passes a new onClose arrow.
    await user.click(middle);
    await user.click(middle);
    expect(middle.textContent).toBe("Mitte 2");
    expect(document.activeElement).toBe(middle);
  });
});
