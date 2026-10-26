// Imports the three Shadow Hearts: Covenant MWo3 code overlays (FIELD.BIN, BATTLE.BIN, OPENING.BIN)
// into the current Ghidra project, one program per overlay.
//
// MWo3 layout (reverse engineered): 0x40-byte header
//   +0x00 "MWo3"  +0x04 overlay id  +0x08 load address (0x45ea80)  +0x0c code size  +0x10 data size
//   +0x14 ?  +0x18/+0x1c end address  +0x20 name
// Everything after the header is loaded flat at the load address; there are no relocations.
//
// Each overlay becomes a COPY of the main program (SLUS_210.41) with the overlay mapped in at
// 0x45ea80, so calls from the overlay into the core game resolve to the already-analysed
// functions and labels.
//
// Run with SLUS_210.41 open (File > Save it first, the copy is made from the saved version).
// Headless: analyzeHeadless <dir> <project> -process SLUS_210.41 -noanalysis
//           -scriptPath <this dir> -postScript ImportMWo3Overlays.java "<ISO Extracted dir>"
//
// @category ShadowHearts

import java.io.ByteArrayInputStream;
import java.io.File;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.util.Arrays;

import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.script.GhidraScript;
import ghidra.framework.model.DomainFile;
import ghidra.framework.model.DomainFolder;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.Program;
import ghidra.program.model.mem.MemoryBlock;

public class ImportMWo3Overlays extends GhidraScript {

	private static final String[] OVERLAYS = { "FIELD.BIN", "BATTLE.BIN", "OPENING.BIN" };
	private static final String DEFAULT_DIR = ""   // optional: folder holding FIELD.BIN / BATTLE.BIN / OPENING.BIN;

	@Override
	public void run() throws Exception {
		if (currentProgram == null) {
			printerr("Open SLUS_210.41 first.");
			return;
		}
		String[] args = getScriptArgs();
		File dir;
		if (args.length > 0) {
			dir = new File(args[0]);
		}
		else if (new File(DEFAULT_DIR).isDirectory()) {
			dir = new File(DEFAULT_DIR);
		}
		else {
			dir = askDirectory("Folder containing FIELD.BIN / BATTLE.BIN / OPENING.BIN", "Select");
		}

		Program main = currentProgram;
		DomainFile mainFile = main.getDomainFile();
		DomainFolder folder = mainFile.getParent();

		for (String name : OVERLAYS) {
			if (monitor.isCancelled()) {
				return;
			}
			File f = new File(dir, name);
			if (!f.isFile()) {
				printerr("Missing " + f);
				continue;
			}
			DomainFile existing = folder.getFile(name);
			if (existing != null) {
				// An earlier raw import (base 0, header included) is wrong; keep it under a new name.
				String old = name + ".raw_import";
				try {
					existing.setName(old);
					println("Renamed existing " + name + " to " + old);
				}
				catch (Exception e) {
					printerr("Cannot rename existing " + name + " (is it open in a tool?): " + e +
						"\nClose it or delete it, then run again.");
					continue;
				}
			}
			importOverlay(mainFile, folder, name, Files.readAllBytes(f.toPath()));
		}
		println("Done.");
	}

	private void importOverlay(DomainFile mainFile, DomainFolder folder, String name, byte[] file)
			throws Exception {
		ByteBuffer bb = ByteBuffer.wrap(file).order(ByteOrder.LITTLE_ENDIAN);
		if (file.length < 0x40 || bb.getInt(0) != 0x336f574d) { // "MWo3"
			printerr(name + ": not an MWo3 file");
			return;
		}
		long loadAddr = bb.getInt(8) & 0xffffffffL;
		int codeSize = bb.getInt(0xc);
		int dataSize = bb.getInt(0x10);
		byte[] body = Arrays.copyOfRange(file, 0x40, file.length);
		println(String.format("%s: id=%d load=0x%x code=0x%x data=0x%x body=0x%x", name,
			bb.getInt(4), loadAddr, codeSize, dataSize, body.length));

		DomainFile tmp = mainFile.copyTo(folder, monitor);
		tmp.setName(name);
		DomainFile copy = folder.getFile(name); // re-lookup, the copyTo handle is stale after rename
		Program p = (Program) copy.getDomainObject(this, true, false, monitor);
		try {
			int tx = p.startTransaction("Import MWo3 overlay " + name);
			boolean ok = false;
			try {
				AddressSpace sp = p.getAddressFactory().getDefaultAddressSpace();
				Address base = sp.getAddress(loadAddr);
				MemoryBlock blk = p.getMemory().createInitializedBlock("OVL_" + name.replace('.', '_'),
					base, new ByteArrayInputStream(body), body.length, monitor, false);
				blk.setRead(true);
				blk.setWrite(true);
				blk.setExecute(true);
				blk.setComment("MWo3 overlay " + name + ", file offset 0x40");
				ok = true;
			}
			finally {
				p.endTransaction(tx, ok);
			}
			if (!ok) {
				return;
			}

			// Create functions at every "addiu/daddiu sp,sp,-N" prologue.
			Address base = p.getAddressFactory().getDefaultAddressSpace().getAddress(loadAddr);
			int made = 0;
			tx = p.startTransaction("Create overlay functions");
			try {
				ByteBuffer w = ByteBuffer.wrap(body).order(ByteOrder.LITTLE_ENDIAN);
				for (int off = 0; off + 4 <= body.length; off += 4) {
					int insn = w.getInt(off);
					int top = insn & 0xffff8000;
					if (top == 0x27bd8000 || top == 0x67bd8000) {
						Address a = base.add(off);
						if (p.getFunctionManager().getFunctionAt(a) == null) {
							new DisassembleCommand(a, null, true).applyTo(p, monitor);
							if (new CreateFunctionCmd(a).applyTo(p, monitor)) {
								made++;
							}
						}
					}
				}
			}
			finally {
				p.endTransaction(tx, true);
			}
			println(name + ": created " + made + " functions");

			p.save("Imported MWo3 overlay " + name, monitor);
			println(name + ": saved, run auto-analysis from the CodeBrowser (Analysis > Auto Analyze)");
		}
		finally {
			p.release(this);
		}
	}
}
