# BOM Cockpit

I want to build a GUI extension for the KicadTools project.

Ultimately, I want to be able to see the data related to the BOM (from
the schematic) and the placement info (from the layout).

Ultimately, I'd like to be able to query Digikey to find part numbers
for parts with no part number.  This functionality should consult a
database of existing inventory first, then query Digikey for a
suitable part.

Once the digikey parts are all assigned, I want to be able to cross
reference with JLCPCB's assembly service to see what their part number
is for the specified part.

For now, I want to start with

* An initial GUI
1) A GUI which provides a menu item to read a schematic
2) A default view which will provide the some info as conveyed by the bom function in kicadtools.
3) A checkbox beside each intent type guess which will mark the guess as valid.
4) A button which will "Accept Checked" and another which will "Accept All"
5) Once accepted, the "Guessed" indicator should change to a checkmark.
6) Finally, the ability to write out a new schematic file with all of the accepted guesses converted to intent type attributes on the symbol so the next read will have fewer guesses.

Make a hierarchical todo list with this as the initial item and the other things I mentioned as additional follow on tasks.

Feel free to make suggestions to improve the workflow.
